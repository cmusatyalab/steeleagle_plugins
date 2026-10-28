package fleet

import (
	"context"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/cmusatyalab/steeleagle/config"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"github.com/rs/zerolog"
)

const (
	v1  = "100.64.1.5.cassowary"
	v2  = "100.64.1.5.harpy"
	gcs = "100.64.1.5.gcs"
)

// flowsFixture is a fake daemon with two vehicles with cameras, and Flows
// connected to it.
func flowsFixture(t *testing.T) (*fakeDaemon, *Flows, Snapshot) {
	t.Helper()
	d := newFakeDaemon(t)
	d.services = []*daemonpb.Service{
		service(v1, daemonpb.ServiceCategory_SERVICE_CATEGORY_VEHICLE, "quadcopter", driverConfig, true),
		service(v2, daemonpb.ServiceCategory_SERVICE_CATEGORY_VEHICLE, "quadcopter", driverConfig, true),
		daemonpb.Service_builder{
			Name: "100.64.1.5.encoder", Category: daemonpb.ServiceCategory_SERVICE_CATEGORY_ENGINE,
			Plugin: EncoderPlugin, Uri: "github.com/cmusatyalab/steeleagle_plugins//defaults/h264", Running: true,
		}.Build(),
	}
	snap, err := List(context.Background(), daemonpb.NewDaemonServiceClient(d.conn()))
	if err != nil {
		t.Fatal(err)
	}
	f := NewFlows(d.conn(), FlowSpec{Engine: gcs}, zerolog.Nop())
	return d, f, snap
}

// wantFlow is the TOML of the flow built for the named vehicle.
func wantFlow(t *testing.T, snap Snapshot, name string) []byte {
	t.Helper()
	for _, v := range snap.Vehicles {
		if v.Name == name {
			flow, _ := BuildFlow(v, snap, FlowSpec{Engine: gcs})
			b, err := flow.ToBytes()
			if err != nil {
				t.Fatal(err)
			}
			return b
		}
	}
	t.Fatalf("no vehicle %s", name)
	return nil
}

// sameFlow reports whether the TOML got is the flow want, ignoring comments
// and formatting.
func sameFlow(t *testing.T, got, want []byte) bool {
	t.Helper()
	g, err := config.ParseFlowConfigFromBytes(got)
	if err != nil {
		t.Fatalf("pushed flow %q: %v", got, err)
	}
	w, err := config.ParseFlowConfigFromBytes(want)
	if err != nil {
		t.Fatal(err)
	}
	return reflect.DeepEqual(g, w)
}

// setCount is how many times SetDataflow was called for the vehicle.
func (d *fakeDaemon) setCount(vehicle string) int {
	d.mu.Lock()
	defer d.mu.Unlock()
	n := 0
	for _, v := range d.setCalls {
		if v == vehicle {
			n++
		}
	}
	return n
}

// fakeClock returns a clock for f, and a function that advances it.
func fakeClock(f *Flows) func(time.Duration) {
	now := time.Unix(1000, 0)
	f.now = func() time.Time { return now }
	return func(d time.Duration) { now = now.Add(d) }
}

func TestObservePushesWhenEmpty(t *testing.T) {
	d, f, snap := flowsFixture(t)
	d.flows[v2] = []byte("x")
	f.Observe(snap)
	f.Wait()
	if !reflect.DeepEqual(d.setCalls, []string{v1}) {
		t.Fatalf("SetDataflow went to %v, want only %s", d.setCalls, v1)
	}
	if !sameFlow(t, d.flows[v1], wantFlow(t, snap, v1)) {
		t.Errorf("pushed flow\n%s\nwant\n%s", d.flows[v1], wantFlow(t, snap, v1))
	}
	if s := f.Status(v1); s != "ok" {
		t.Errorf("Status(v1) = %q, want ok", s)
	}
	if s := f.Status(v2); !strings.HasPrefix(s, "not pushed: vehicle runs a flow that can't be parsed") {
		t.Errorf("Status(v2) = %q, want not pushed", s)
	}
}

func TestObserveOnce(t *testing.T) {
	d, f, snap := flowsFixture(t)
	fakeClock(f)
	f.Observe(snap)
	f.Observe(snap)
	f.Wait()
	if len(d.getCalls) != 2 {
		t.Errorf("GetFlow calls = %v, want one per vehicle", d.getCalls)
	}
}

func TestObserveChecksAgainAfterReturn(t *testing.T) {
	d, f, snap := flowsFixture(t)
	fakeClock(f)
	f.Observe(snap)
	f.Wait()
	f.Observe(Snapshot{})
	f.Observe(snap)
	f.Wait()
	if len(d.getCalls) != 4 {
		t.Errorf("GetFlow calls = %v, want two rounds", d.getCalls)
	}
}

func TestObserveRetriesFailedCheck(t *testing.T) {
	d, f, snap := flowsFixture(t)
	advance := fakeClock(f)
	d.getFail[v1] = 1
	f.Observe(snap)
	f.Wait()
	if len(d.setCalls) != 1 || d.setCalls[0] != v2 {
		t.Fatalf("SetDataflow went to %v, want only %s", d.setCalls, v2)
	}
	advance(retryMin - time.Millisecond)
	f.Observe(snap)
	f.Wait()
	if len(d.getCalls) != 2 {
		t.Fatalf("GetFlow calls = %v, want no retry within the backoff", d.getCalls)
	}
	advance(time.Millisecond)
	f.Observe(snap)
	f.Wait()
	if d.setCount(v1) != 1 {
		t.Fatalf("SetDataflow went to %v, want %s retried", d.setCalls, v1)
	}
	if s := f.Status(v1); s != "ok" {
		t.Errorf("Status(v1) = %q, want ok", s)
	}
}

// TestObserveRetriesUntilData checks that the flow is pushed again, with
// backoff, until the vehicle's telemetry and frames arrive, and not after.
func TestObserveRetriesUntilData(t *testing.T) {
	d, f, snap := flowsFixture(t)
	advance := fakeClock(f)
	f.Observe(snap)
	f.Wait()
	first := d.flows[v1]

	// Not before the backoff
	advance(retryMin - time.Millisecond)
	f.Observe(snap)
	f.Wait()
	if n := d.setCount(v1); n != 1 {
		t.Fatalf("pushes = %d within the backoff, want 1", n)
	}
	// Then again, with different bytes, so the daemon sets it again
	advance(time.Millisecond)
	f.Observe(snap)
	f.Wait()
	if n := d.setCount(v1); n != 2 {
		t.Fatalf("pushes = %d after the backoff, want 2", n)
	}
	if string(d.flows[v1]) == string(first) {
		t.Errorf("retry pushed the same bytes %q", first)
	}
	if !sameFlow(t, d.flows[v1], wantFlow(t, snap, v1)) {
		t.Errorf("retry pushed\n%s", d.flows[v1])
	}
	// The backoff doubles
	advance(2*retryMin - time.Millisecond)
	f.Observe(snap)
	f.Wait()
	if n := d.setCount(v1); n != 2 {
		t.Fatalf("pushes = %d within the doubled backoff, want 2", n)
	}

	// Telemetry alone is not enough when the flow carries video
	f.ReceivedTelemetry(v1, testTelemetry(time.Now(), 10, 0))
	advance(time.Millisecond)
	f.Observe(snap)
	f.Wait()
	if n := d.setCount(v1); n != 3 {
		t.Fatalf("pushes = %d with telemetry but no frames, want 3", n)
	}
	// Once both arrive, it is left alone
	advance(retryMax)
	f.ReceivedTelemetry(v1, testTelemetry(time.Now(), 10, 0))
	f.ReceivedFrame(v1)
	f.Observe(snap)
	f.Wait()
	if n := d.setCount(v1); n != 3 {
		t.Fatalf("pushes = %d with data arriving, want 3", n)
	}
	// Nor when they stop later: a mission may have changed the flow
	advance(freshFor)
	f.Observe(snap)
	f.Wait()
	if n := d.setCount(v1); n != 3 {
		t.Fatalf("pushes = %d after data stopped, want 3", n)
	}
}

// TestObserveTelemetryEnoughWithoutVideo checks that a vehicle whose flow
// can't carry video only needs its telemetry to arrive.
func TestObserveTelemetryEnoughWithoutVideo(t *testing.T) {
	d, f, snap := flowsFixture(t)
	advance := fakeClock(f)
	snap.Encoders = map[string]string{}
	f.ReceivedTelemetry(v1, testTelemetry(time.Now(), 10, 0))
	f.Observe(snap)
	f.Wait()
	if n := d.setCount(v1); n != 0 {
		t.Fatalf("pushes = %d with telemetry arriving, want 0", n)
	}
	advance(retryMin)
	f.ReceivedTelemetry(v1, testTelemetry(time.Now(), 10, 0))
	f.Observe(snap)
	f.Wait()
	if n := d.setCount(v1); n != 0 {
		t.Fatalf("pushes = %d with telemetry still arriving, want 0", n)
	}
}

// TestHoldRearms checks that a hold pushes the flow, and keeps pushing it
// until the vehicle's data arrives again.
func TestHoldRearms(t *testing.T) {
	d, f, snap := flowsFixture(t)
	advance := fakeClock(f)
	f.ReceivedTelemetry(v1, testTelemetry(time.Now(), 10, 0))
	f.ReceivedFrame(v1)
	f.Observe(snap)
	f.Wait()
	advance(freshFor)
	f.Observe(snap)
	f.Wait()
	if n := d.setCount(v1); n != 0 {
		t.Fatalf("pushes = %d before the hold, want 0", n)
	}
	f.PushAfterHold([]string{v1})
	f.Wait()
	if n := d.setCount(v1); n != 1 {
		t.Fatalf("pushes = %d after the hold, want 1", n)
	}
	advance(retryMin)
	f.Observe(snap)
	f.Wait()
	if n := d.setCount(v1); n != 2 {
		t.Fatalf("pushes = %d while data isn't arriving after the hold, want 2", n)
	}
	advance(retryMax)
	f.ReceivedTelemetry(v1, testTelemetry(time.Now(), 10, 0))
	f.ReceivedFrame(v1)
	f.Observe(snap)
	f.Wait()
	advance(retryMax)
	f.Observe(snap)
	f.Wait()
	if n := d.setCount(v1); n != 2 {
		t.Fatalf("pushes = %d once data arrived, want 2", n)
	}
}

// TestMissionStartedStopsRetries checks that once a vehicle's mission starts,
// its flow is no longer pushed, even if its data never arrived.
func TestMissionStartedStopsRetries(t *testing.T) {
	d, f, snap := flowsFixture(t)
	advance := fakeClock(f)
	f.Observe(snap)
	f.Wait()
	f.MissionStarted([]string{v1})
	for range 3 {
		advance(retryMax)
		f.Observe(snap)
		f.Wait()
	}
	if n := d.setCount(v1); n != 1 {
		t.Errorf("pushes = %d after the mission started, want 1", n)
	}
	if n := d.setCount(v2); n != 4 {
		t.Errorf("pushes to the other vehicle = %d, want 4", n)
	}
}

// TestObserveLeavesMissionFlow checks that a flow with engines other than the
// GCS and the encoder, a mission's, is never replaced.
func TestObserveLeavesMissionFlow(t *testing.T) {
	d, f, snap := flowsFixture(t)
	advance := fakeClock(f)
	mission := []byte(`[[producers]]
name = "telemetry"

[[engines]]
name = "100.64.1.5.yolo"
location = "local"

[[edges]]
from = "telemetry"
to = "100.64.1.5.yolo"
timeout = "2s"
`)
	d.flows[v1] = mission
	for range 3 {
		f.Observe(snap)
		f.Wait()
		advance(retryMax)
	}
	if n := d.setCount(v1); n != 0 {
		t.Errorf("pushes = %d over a mission flow, want 0", n)
	}
	if string(d.flows[v1]) != string(mission) {
		t.Errorf("flow = %s, want the mission's", d.flows[v1])
	}
	if s, want := f.Status(v1), "not pushed: vehicle runs another flow, with engine 100.64.1.5.yolo"; s != want {
		t.Errorf("Status = %q, want %q", s, want)
	}
}

// TestObserveReplacesTelemetryOnlyFlow checks that a GCS flow without video,
// pushed before the encoder was found, is replaced by one with video.
func TestObserveReplacesTelemetryOnlyFlow(t *testing.T) {
	d, f, snap := flowsFixture(t)
	fakeClock(f)
	noEnc := snap
	noEnc.Encoders = map[string]string{}
	old, _ := BuildFlow(snap.Vehicles[0], noEnc, FlowSpec{Engine: gcs})
	b, err := old.ToBytes()
	if err != nil {
		t.Fatal(err)
	}
	d.flows[v1] = b
	f.Observe(snap)
	f.Wait()
	if !sameFlow(t, d.flows[v1], wantFlow(t, snap, v1)) {
		t.Errorf("flow = %s, want the GCS flow with video", d.flows[v1])
	}
}

func TestPushAfterHoldOverwrites(t *testing.T) {
	d, f, snap := flowsFixture(t)
	d.flows[v1] = []byte("mission")
	f.Observe(snap)
	f.Wait()
	d.mu.Lock()
	d.flows[v1] = []byte("mission")
	d.mu.Unlock()
	f.PushAfterHold([]string{v1})
	f.Wait()
	if !sameFlow(t, d.flows[v1], wantFlow(t, snap, v1)) {
		t.Errorf("flow = %s, want the GCS flow", d.flows[v1])
	}
}

func TestPushListsWhenNoSnapshot(t *testing.T) {
	d, f, snap := flowsFixture(t)
	f.PushAfterHold([]string{v1})
	f.Wait()
	if !sameFlow(t, d.flows[v1], wantFlow(t, snap, v1)) {
		t.Errorf("flow = %s, want the GCS flow", d.flows[v1])
	}
}

func TestPushStatusError(t *testing.T) {
	d, f, snap := flowsFixture(t)
	d.setErr = status.Error(codes.InvalidArgument, "bad flow")
	f.Observe(snap)
	f.Wait()
	if s := f.Status(v1); s != "error: bad flow" {
		t.Errorf("Status = %q, want %q", s, "error: bad flow")
	}
}

func TestPushNoVideoStatus(t *testing.T) {
	d, f, _ := flowsFixture(t)
	d.services = []*daemonpb.Service{
		service(v1, daemonpb.ServiceCategory_SERVICE_CATEGORY_VEHICLE, "quadcopter", "[plugin]\nname = \"quadcopter\"\ntype = \"driver\"\n\n[driver]\ntype = \"copter\"\n", true),
	}
	f.PushAfterHold([]string{v1})
	f.Wait()
	if s := f.Status(v1); s != "no video: vehicle has no cameras" {
		t.Errorf("Status = %q", s)
	}
}

func TestPushCoalesces(t *testing.T) {
	d, f, _ := flowsFixture(t)
	d.setGate = make(chan struct{})
	f.PushAfterHold([]string{v1})
	f.PushAfterHold([]string{v1})
	f.PushAfterHold([]string{v1})
	close(d.setGate)
	f.Wait()
	if len(d.setCalls) != 2 {
		t.Errorf("SetDataflow called %d times, want 2", len(d.setCalls))
	}
}

// TestObserveRidesOutUnavailableDaemon checks that a vehicle whose daemon is
// briefly listed as unavailable is not taken as reconnecting when it comes
// back: it keeps its backoff, and isn't checked while its daemon is away.
func TestObserveRidesOutUnavailableDaemon(t *testing.T) {
	d, f, snap := flowsFixture(t)
	advance := fakeClock(f)
	away := Snapshot{
		Engines: map[string]bool{}, Encoders: map[string]string{},
		Unavailable: []Unavailable{{IP: "100.64.1.5", Hostname: "swap03", Reason: "deadline exceeded"}},
	}
	f.Observe(snap)
	f.Wait()
	if n := d.setCount(v1); n != 1 {
		t.Fatalf("pushes = %d on connect, want 1", n)
	}
	for range 3 {
		advance(retryMin)
		f.Observe(away)
		f.Wait()
		f.Observe(snap)
		f.Wait()
	}
	// One check per backoff period: retryMin, then 2*retryMin from there
	if n := d.setCount(v1); n != 3 {
		t.Errorf("pushes = %d while the daemon flapped, want 3 (not reset by each return)", n)
	}
	// A push while the daemon is away still uses the vehicle as last listed
	f.Observe(away)
	f.PushAfterHold([]string{v1})
	f.Wait()
	if s := f.Status(v1); s != "ok" {
		t.Errorf("Status after a hold while away = %q, want ok", s)
	}
}
