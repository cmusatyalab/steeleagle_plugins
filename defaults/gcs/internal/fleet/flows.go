package fleet

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/cmusatyalab/steeleagle/config"

	"github.com/rs/zerolog"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	vehiclepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/vehicle"
)

const (
	// routeHeader is the metadata key the daemon routes calls to vehicles by.
	routeHeader = "x-route-key"
	// pushTimeout bounds each push, and each check.
	pushTimeout = 15 * time.Second
	// freshFor is how long after a vehicle's last telemetry, or frame, its
	// data still counts as arriving. It matches when the UI shows a vehicle
	// as disconnected.
	freshFor = 5 * time.Second
	// retryMin and retryMax bound the wait between checks of a vehicle whose
	// data isn't arriving. The wait starts at retryMin and doubles with each
	// check, until the data arrives.
	retryMin = 2 * time.Second
	retryMax = 30 * time.Second
)

// vehicleState is what Flows tracks about a vehicle. Flows.mu guards it.
type vehicleState struct {
	inFlight bool   // a push is running
	again    bool   // push once more when the running push is done
	status   string // see Flows.Status
	checking bool   // a check is running
	// present is whether the vehicle was in the last snapshot.
	present bool
	// armed is whether the vehicle is checked until its data arrives: from
	// when it connects, or is held, until its data arrives or a mission
	// starts on it.
	armed bool
	// attempts is how many checks have run since the vehicle was armed;
	// nextCheck is when the next may run.
	attempts  int
	nextCheck time.Time
	// lastTelemetry and lastFrame are when the GCS last received the
	// vehicle's telemetry and frames.
	lastTelemetry, lastFrame time.Time
}

// Flows pushes the GCS dataflow to vehicles, and tracks how each push went.
type Flows struct {
	daemon  daemonpb.DaemonServiceClient
	data    vehiclepb.DataServiceClient
	spec    FlowSpec
	log     zerolog.Logger
	pending sync.WaitGroup
	now     func() time.Time // the clock, replaced by tests

	mu       sync.Mutex
	snap     Snapshot
	hasSnap  bool
	vehicles map[string]*vehicleState
}

// NewFlows returns a Flows that talks to the daemon over conn.
func NewFlows(conn grpc.ClientConnInterface, spec FlowSpec, log zerolog.Logger) *Flows {
	return &Flows{
		daemon:   daemonpb.NewDaemonServiceClient(conn),
		data:     vehiclepb.NewDataServiceClient(conn),
		spec:     spec,
		log:      log,
		now:      time.Now,
		vehicles: map[string]*vehicleState{},
	}
}

// state returns the vehicle's state, creating it. The caller holds f.mu.
func (f *Flows) state(name string) *vehicleState {
	st := f.vehicles[name]
	if st == nil {
		st = &vehicleState{}
		f.vehicles[name] = st
	}
	return st
}

// Received records that the GCS received the vehicle's telemetry, or one of
// its frames if frame is set.
func (f *Flows) Received(vehicle string, frame bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	st := f.state(vehicle)
	if frame {
		st.lastFrame = f.now()
	} else {
		st.lastTelemetry = f.now()
	}
}

// Observe records the latest snapshot, and checks in the background each
// armed vehicle whose data isn't arriving: its telemetry, and its frames if
// its GCS flow carries video. A vehicle is armed when it connects (appears in
// a snapshot, or reappears), and after a hold, until its data arrives or a
// mission starts on it; a vehicle running a mission is never checked. A check
// pushes the GCS flow unless the vehicle runs another flow, a mission's.
// Checks of a vehicle back off from retryMin to retryMax.
func (f *Flows) Observe(snap Snapshot) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.snap, f.hasSnap = snap, true
	present := map[string]bool{}
	now := f.now()
	for _, v := range snap.Vehicles {
		present[v.Name] = true
		st := f.state(v.Name)
		if !st.present {
			st.present = true
			st.arm()
		}
		if !st.armed {
			continue
		}
		_, noVideo := BuildFlow(v, snap, f.spec)
		if arriving(st, noVideo == "", now) {
			st.armed = false
			continue
		}
		if st.checking || st.inFlight || now.Before(st.nextCheck) {
			continue
		}
		st.attempts++
		st.nextCheck = now.Add(min(retryMin<<min(st.attempts-1, 8), retryMax))
		st.checking = true
		f.pending.Add(1)
		go f.check(v, snap, st.attempts)
	}
	for name, st := range f.vehicles {
		if !present[name] {
			st.present = false
		}
	}
}

// arm arms the vehicle, to be checked right away.
func (st *vehicleState) arm() {
	st.armed, st.attempts, st.nextCheck = true, 0, time.Time{}
}

// MissionStarted disarms each vehicle, so that its mission's flow isn't
// replaced.
func (f *Flows) MissionStarted(vehicles []string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, name := range vehicles {
		f.state(name).armed = false
	}
}

// arriving reports whether the vehicle's telemetry, and its frames if video
// is set, arrived within freshFor of now.
func arriving(st *vehicleState, video bool, now time.Time) bool {
	fresh := func(t time.Time) bool { return now.Sub(t) < freshFor }
	return fresh(st.lastTelemetry) && (!video || fresh(st.lastFrame))
}

// check pushes the GCS flow to the vehicle, unless it runs another flow.
func (f *Flows) check(v Vehicle, snap Snapshot, attempt int) {
	defer f.pending.Done()
	defer func() {
		f.mu.Lock()
		f.state(v.Name).checking = false
		f.mu.Unlock()
	}()
	ctx, cancel := context.WithTimeout(context.Background(), pushTimeout)
	resp, err := f.data.GetFlow(route(ctx, v.Name), &vehiclepb.GetFlowRequest{})
	cancel()
	if err != nil {
		f.log.Warn().Err(err).Str("vehicle", v.Name).Msg("could not read the vehicle's flow")
		return
	}
	f.mu.Lock()
	armed := f.state(v.Name).armed
	f.mu.Unlock()
	if !armed {
		return // a mission started meanwhile
	}
	if !f.isGCSFlow(resp.GetToml(), v, snap) {
		f.log.Debug().Str("vehicle", v.Name).Msg("vehicle runs another flow; not pushing the GCS flow")
		return
	}
	if attempt > 1 {
		f.log.Info().Str("vehicle", v.Name).Int("attempt", attempt).
			Msg("vehicle's data isn't arriving; pushing the GCS flow again")
	}
	f.push(v.Name)
}

// isGCSFlow reports whether toml is empty or a GCS flow: one whose engines
// are only the GCS engine and the encoder on the vehicle's daemon. Any other
// flow is a mission's, which a check leaves alone.
func (f *Flows) isGCSFlow(toml []byte, v Vehicle, snap Snapshot) bool {
	if len(toml) == 0 {
		return true
	}
	flow, err := config.ParseFlowConfigFromBytes(toml)
	if err != nil {
		return false
	}
	encoder, hasEncoder := snap.Encoders[v.DaemonIP]
	for _, e := range flow.Engines {
		if e.Name != f.spec.Engine && (!hasEncoder || e.Name != encoder) {
			return false
		}
	}
	return true
}

// PushAfterHold pushes the GCS flow to each vehicle in the background, and
// returns immediately, and arms it to be checked until its data arrives. If a
// push to a vehicle is already running, it runs once more when it is done,
// instead of in parallel.
func (f *Flows) PushAfterHold(vehicles []string) {
	f.mu.Lock()
	now := f.now()
	for _, name := range vehicles {
		// This push is the first attempt
		st := f.state(name)
		st.arm()
		st.attempts, st.nextCheck = 1, now.Add(retryMin)
	}
	f.mu.Unlock()
	for _, name := range vehicles {
		f.push(name)
	}
}

// push starts a push to the vehicle, or asks the running one to repeat.
func (f *Flows) push(name string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	st := f.state(name)
	if st.inFlight {
		st.again = true
		return
	}
	st.inFlight = true
	f.pending.Add(1)
	go f.pushLoop(name)
}

// pushLoop pushes to the vehicle until no one has asked for another push.
func (f *Flows) pushLoop(name string) {
	defer f.pending.Done()
	for {
		result := f.pushOnce(name)
		f.mu.Lock()
		st := f.state(name)
		st.status = result
		if !st.again {
			st.inFlight = false
			f.mu.Unlock()
			return
		}
		st.again = false
		f.mu.Unlock()
	}
}

// pushOnce builds and sets the vehicle's flow, and returns the resulting
// status.
func (f *Flows) pushOnce(name string) string {
	ctx, cancel := context.WithTimeout(context.Background(), pushTimeout)
	defer cancel()

	f.mu.Lock()
	snap, hasSnap := f.snap, f.hasSnap
	f.mu.Unlock()
	if !hasSnap {
		var err error
		if snap, err = List(ctx, f.daemon); err != nil {
			f.log.Warn().Err(err).Str("vehicle", name).Msg("could not list services to push a flow")
			return "error: " + status.Convert(err).Message()
		}
	}
	var vehicle *Vehicle
	for i := range snap.Vehicles {
		if snap.Vehicles[i].Name == name {
			vehicle = &snap.Vehicles[i]
		}
	}
	if vehicle == nil {
		return "error: vehicle is not running"
	}
	flow, noVideo := BuildFlow(*vehicle, snap, f.spec)
	toml, err := flow.ToBytes()
	if err == nil {
		// The daemon ignores a flow that is byte for byte the one it runs,
		// so each check's push differs, to set the flow again
		f.mu.Lock()
		attempt := f.state(name).attempts
		f.mu.Unlock()
		toml = append([]byte(fmt.Sprintf("# GCS flow, attempt %d\n", attempt)), toml...)
		_, err = f.data.SetDataflow(route(ctx, name), vehiclepb.SetDataflowRequest_builder{Toml: toml}.Build())
	}
	if err != nil {
		f.log.Warn().Err(err).Str("vehicle", name).Msg("could not set the vehicle's flow")
		return "error: " + status.Convert(err).Message()
	}
	if noVideo != "" {
		return "no video: " + noVideo
	}
	return "ok"
}

// Status says how the last push to the vehicle went: "" if unknown or still
// pending, "ok" if it set a flow with video, "no video: <reason>" if it set
// a telemetry-only flow, or "error: <message>" if it failed.
func (f *Flows) Status(vehicle string) string {
	f.mu.Lock()
	defer f.mu.Unlock()
	if st := f.vehicles[vehicle]; st != nil {
		return st.status
	}
	return ""
}

// Wait waits for every background check and push to finish. It is for tests.
func (f *Flows) Wait() { f.pending.Wait() }

// route returns ctx with calls routed to the named vehicle.
func route(ctx context.Context, vehicle string) context.Context {
	return metadata.AppendToOutgoingContext(ctx, routeHeader, vehicle)
}
