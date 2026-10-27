package fleet

import (
	"context"
	"sync"
	"time"

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
	// pushTimeout bounds each push, and each initial check.
	pushTimeout = 15 * time.Second
	// checkBackoff is how long after a failed initial check it is retried.
	checkBackoff = 2 * time.Second
)

// vehicleState is what Flows tracks about a vehicle. Flows.mu guards it.
type vehicleState struct {
	inFlight bool   // a push is running
	again    bool   // push once more when the running push is done
	status   string // see Flows.Status
	checked  bool   // the initial check has started
	// retryAt is when the initial check may be retried, after it failed.
	retryAt time.Time
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

// Observe records the latest snapshot. A vehicle that has not been seen
// before, or that has disappeared from a snapshot and come back, gets an
// initial check in the background: if it has no flow, it is pushed the GCS
// flow. A check that fails is retried by a later Observe, at most once every
// checkBackoff.
func (f *Flows) Observe(snap Snapshot) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.snap, f.hasSnap = snap, true
	present := map[string]bool{}
	now := f.now()
	for _, v := range snap.Vehicles {
		present[v.Name] = true
		if st := f.state(v.Name); !st.checked && !now.Before(st.retryAt) {
			st.checked = true
			f.pending.Add(1)
			go f.check(v.Name)
		}
	}
	for name, st := range f.vehicles {
		if !present[name] {
			st.checked = false
		}
	}
}

// check pushes the GCS flow to the vehicle if it has none.
func (f *Flows) check(name string) {
	defer f.pending.Done()
	ctx, cancel := context.WithTimeout(context.Background(), pushTimeout)
	resp, err := f.data.GetFlow(route(ctx, name), &vehiclepb.GetFlowRequest{})
	cancel()
	if err != nil {
		f.log.Warn().Err(err).Str("vehicle", name).Msg("could not read the vehicle's flow")
		f.mu.Lock()
		st := f.state(name)
		st.checked, st.retryAt = false, f.now().Add(checkBackoff)
		f.mu.Unlock()
		return
	}
	if len(resp.GetToml()) == 0 {
		f.push(name)
	}
}

// PushAfterHold pushes the GCS flow to each vehicle in the background, and
// returns immediately. If a push to a vehicle is already running, it runs
// once more when it is done, instead of in parallel.
func (f *Flows) PushAfterHold(vehicles []string) {
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
