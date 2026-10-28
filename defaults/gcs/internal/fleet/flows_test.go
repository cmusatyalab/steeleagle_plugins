package fleet

import (
	"context"
	"reflect"
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
		service("100.64.1.5.h264_encoder", daemonpb.ServiceCategory_SERVICE_CATEGORY_ENGINE, "h264", "", true),
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

func TestObservePushesWhenEmpty(t *testing.T) {
	d, f, snap := flowsFixture(t)
	d.flows[v2] = []byte("x")
	f.Observe(snap)
	f.Wait()
	if !reflect.DeepEqual(d.setCalls, []string{v1}) {
		t.Fatalf("SetDataflow went to %v, want only %s", d.setCalls, v1)
	}
	got, err := config.ParseFlowConfigFromBytes(d.flows[v1])
	if err != nil {
		t.Fatal(err)
	}
	gotB, _ := got.ToBytes()
	if !reflect.DeepEqual(gotB, wantFlow(t, snap, v1)) {
		t.Errorf("pushed flow\n%s\nwant\n%s", gotB, wantFlow(t, snap, v1))
	}
	if s := f.Status(v1); s != "ok" {
		t.Errorf("Status(v1) = %q, want ok", s)
	}
	if s := f.Status(v2); s != "" {
		t.Errorf("Status(v2) = %q, want empty", s)
	}
}

func TestObserveOnce(t *testing.T) {
	d, f, snap := flowsFixture(t)
	f.Observe(snap)
	f.Observe(snap)
	f.Wait()
	if len(d.getCalls) != 2 {
		t.Errorf("GetFlow calls = %v, want one per vehicle", d.getCalls)
	}
}

func TestObserveChecksAgainAfterReturn(t *testing.T) {
	d, f, snap := flowsFixture(t)
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
	now := time.Unix(1000, 0)
	f.now = func() time.Time { return now }
	d.getFail[v1] = 1
	f.Observe(snap)
	f.Wait()
	if len(d.setCalls) != 1 || d.setCalls[0] != v2 {
		t.Fatalf("SetDataflow went to %v, want only %s", d.setCalls, v2)
	}
	// Within the backoff, the failed check isn't retried
	now = now.Add(checkBackoff - time.Millisecond)
	f.Observe(snap)
	f.Wait()
	if len(d.getCalls) != 2 {
		t.Fatalf("GetFlow calls = %v, want no retry within the backoff", d.getCalls)
	}
	now = now.Add(time.Millisecond)
	f.Observe(snap)
	f.Wait()
	if !reflect.DeepEqual(d.setCalls, []string{v2, v1}) {
		t.Fatalf("SetDataflow went to %v, want %s retried", d.setCalls, v1)
	}
	if s := f.Status(v1); s != "ok" {
		t.Errorf("Status(v1) = %q, want ok", s)
	}
	// Once it succeeds, it isn't checked again
	now = now.Add(checkBackoff)
	f.Observe(snap)
	f.Wait()
	if len(d.getCalls) != 3 {
		t.Errorf("GetFlow calls = %v, want three", d.getCalls)
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
	if !reflect.DeepEqual(d.flows[v1], wantFlow(t, snap, v1)) {
		t.Errorf("flow = %s, want the GCS flow", d.flows[v1])
	}
}

func TestPushListsWhenNoSnapshot(t *testing.T) {
	d, f, snap := flowsFixture(t)
	f.PushAfterHold([]string{v1})
	f.Wait()
	if !reflect.DeepEqual(d.flows[v1], wantFlow(t, snap, v1)) {
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
