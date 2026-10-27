package main

import (
	"context"
	"net"
	"slices"
	"sync"
	"testing"
	"time"

	telemetrypb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/telemetry"
	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	driverpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/driver"
	missionpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/mission"
	vehiclepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/vehicle"
	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"github.com/rs/zerolog"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
)

const (
	alphaName   = "100.64.0.1.alpha"
	bravoName   = "100.64.0.2.bravo"
	charlieName = "100.64.0.3.charlie"
)

type applied struct {
	vehicle string
	flow    *config.FlowConfig
}

type recordingFlows struct {
	mu    sync.Mutex
	calls []applied
}

func (r *recordingFlows) Apply(vehicle string, flow *config.FlowConfig) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.calls = append(r.calls, applied{vehicle, flow})
	return nil
}

func (r *recordingFlows) snapshot() []applied {
	r.mu.Lock()
	defer r.mu.Unlock()
	return slices.Clone(r.calls)
}

type harness struct {
	conn     *grpc.ClientConn
	vehicles map[string]*Vehicle
	flows    *recordingFlows
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	names := []string{alphaName, bravoName, charlieName}
	vehicles := map[string]*Vehicle{}
	for i, n := range names {
		vehicles[n] = NewVehicle(n, LatLon{40.4433 + float64(i)*1e-4, -79.9436})
	}
	flows := &recordingFlows{}
	top := Topology{
		GCSIP:          "100.64.0.9",
		Vehicles:       names,
		EncoderDaemons: map[string]bool{"100.64.0.1": true, "100.64.0.3": true},
	}
	d := NewDaemon(top, vehicles, flows, zerolog.Nop())
	ln := bufconn.Listen(1 << 20)
	s := grpc.NewServer()
	d.Register(s)
	go s.Serve(ln)
	t.Cleanup(s.Stop)
	conn, err := grpc.NewClient("passthrough:///d",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return ln.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return &harness{conn, vehicles, flows}
}

func routed(key string) context.Context {
	return metadata.AppendToOutgoingContext(context.Background(), "x-route-key", key)
}

func TestListServices(t *testing.T) {
	h := newHarness(t)
	resp, err := daemonpb.NewDaemonServiceClient(h.conn).ListServices(context.Background(), &daemonpb.ListServicesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	engines := map[string]*daemonpb.Service{}
	vehicles := 0
	for _, s := range resp.GetServices() {
		if s.GetCategory() == daemonpb.ServiceCategory_SERVICE_CATEGORY_ENGINE {
			engines[s.GetName()] = s
		} else if s.GetCategory() == daemonpb.ServiceCategory_SERVICE_CATEGORY_VEHICLE {
			vehicles++
			if s.GetPlugin() != "mock_vehicle" || !s.GetRunning() {
				t.Errorf("vehicle %s: plugin %q running %v", s.GetName(), s.GetPlugin(), s.GetRunning())
			}
		}
	}
	if vehicles != 3 {
		t.Errorf("got %d vehicles, want 3", vehicles)
	}
	for _, n := range []string{"100.64.0.1.h264_encoder", "100.64.0.3.h264_encoder", "100.64.0.9.gcs"} {
		if engines[n] == nil {
			t.Errorf("missing engine %s in %v", n, engines)
		}
	}
	if engines["100.64.0.2.h264_encoder"] != nil {
		t.Error("bravo's daemon has no encoder")
	}
	if g := engines["100.64.0.9.gcs"]; g != nil && (g.GetPlugin() != "gcs" || g.GetAuth() != "admin") {
		t.Errorf("gcs engine = %v", g)
	}

	snap, err := fleet.List(context.Background(), daemonpb.NewDaemonServiceClient(h.conn))
	if err != nil {
		t.Fatal(err)
	}
	if len(snap.Vehicles) != 3 {
		t.Fatalf("fleet saw %d vehicles", len(snap.Vehicles))
	}
	for _, v := range snap.Vehicles {
		if v.DriverErr != nil {
			t.Fatalf("%s: %v", v.Name, v.DriverErr)
		}
		c := v.Driver.Cameras[0]
		if c.Name != "front" || !c.Encode || !c.Primary {
			t.Errorf("%s camera = %+v", v.Name, c)
		}
	}
	want := map[string]string{alphaName: "", bravoName: "no video", charlieName: ""}
	for _, v := range snap.Vehicles {
		_, noVideo := fleet.BuildFlow(v, snap, fleet.FlowSpec{Engine: "100.64.0.9.gcs"})
		if (want[v.Name] == "") != (noVideo == "") {
			t.Errorf("%s: noVideo = %q", v.Name, noVideo)
		}
	}
}

func TestRouting(t *testing.T) {
	h := newHarness(t)
	c := driverpb.NewControlServiceClient(h.conn)
	alt := float32(10)
	if _, err := c.TakeOff(routed(alphaName), driverpb.TakeOffRequest_builder{Altitude: &alt}.Build()); err != nil {
		t.Fatal(err)
	}
	if m := h.vehicles[alphaName].Telemetry(time.Now()).GetPositionInfo(); m == nil {
		t.Fatal("no position")
	}
	h.vehicles[alphaName].Step(1e9, time.Now())
	if z := h.vehicles[alphaName].Telemetry(time.Now()).GetPositionInfo().GetRelativePosition().GetZ(); z <= 0 {
		t.Errorf("alpha did not climb: %v", z)
	}
	h.vehicles[bravoName].Step(1e9, time.Now())
	if z := h.vehicles[bravoName].Telemetry(time.Now()).GetPositionInfo().GetRelativePosition().GetZ(); z != 0 {
		t.Errorf("bravo moved: %v", z)
	}
	_, err := c.Land(context.Background(), &driverpb.LandRequest{})
	if status.Code(err) != codes.NotFound {
		t.Errorf("missing key: %v", err)
	}
	_, err = c.Land(routed("nope"), &driverpb.LandRequest{})
	if status.Code(err) != codes.NotFound || status.Convert(err).Message() != `no route for key "nope"` {
		t.Errorf("unknown key: %v", err)
	}
}

const gcsFlow = `
[[producers]]
name = "telemetry"
[[producers]]
name = "front"
[[engines]]
name = "100.64.0.1.h264_encoder"
location = "remote"
[[engines]]
name = "100.64.0.9.gcs"
location = "remote"
[[edges]]
from = "front"
to = "100.64.0.1.h264_encoder"
timeout = "2s"
[[edges]]
from = "100.64.0.1.h264_encoder"
to = "100.64.0.9.gcs"
timeout = "2s"
[[edges]]
from = "telemetry"
to = "100.64.0.9.gcs"
timeout = "2s"
`

func TestSetDataflowQualifies(t *testing.T) {
	h := newHarness(t)
	dc := vehiclepb.NewDataServiceClient(h.conn)
	if _, err := dc.SetDataflow(routed(alphaName), vehiclepb.SetDataflowRequest_builder{Toml: []byte(gcsFlow)}.Build()); err != nil {
		t.Fatal(err)
	}
	calls := h.flows.snapshot()
	if len(calls) != 1 || calls[0].vehicle != alphaName {
		t.Fatalf("calls = %+v", calls)
	}
	f := calls[0].flow
	var prods []string
	for _, p := range f.Producers {
		prods = append(prods, p.Name)
	}
	if !slices.Equal(prods, []string{alphaName + ".telemetry", alphaName + ".front"}) {
		t.Errorf("producers = %v", prods)
	}
	var froms []string
	for _, e := range f.Edges {
		froms = append(froms, e.From)
	}
	if !slices.Contains(froms, alphaName+".front") || !slices.Contains(froms, alphaName+".telemetry") {
		t.Errorf("edges not renamed: %v", froms)
	}
	for _, e := range f.Engines {
		if e.Location != config.LocationLocal {
			t.Errorf("engine %s location %s", e.Name, e.Location)
		}
	}
	g, err := dc.GetFlow(routed(alphaName), &vehiclepb.GetFlowRequest{})
	if err != nil || string(g.GetToml()) != gcsFlow {
		t.Errorf("GetFlow = %q, %v", g.GetToml(), err)
	}
	// Identical bytes are a no-op.
	if _, err := dc.SetDataflow(routed(alphaName), vehiclepb.SetDataflowRequest_builder{Toml: []byte(gcsFlow)}.Build()); err != nil {
		t.Fatal(err)
	}
	if n := len(h.flows.snapshot()); n != 1 {
		t.Errorf("identical flow applied again: %d calls", n)
	}
	// Other vehicles have none.
	g, _ = dc.GetFlow(routed(bravoName), &vehiclepb.GetFlowRequest{})
	if len(g.GetToml()) != 0 {
		t.Error("bravo has a flow")
	}
	_, err = dc.SetDataflow(routed(alphaName), vehiclepb.SetDataflowRequest_builder{Toml: []byte("not toml [")}.Build())
	if status.Code(err) != codes.InvalidArgument {
		t.Errorf("invalid flow: %v", err)
	}
}

func TestStartMissionReplacesFlow(t *testing.T) {
	h := newHarness(t)
	dc := vehiclepb.NewDataServiceClient(h.conn)
	dc.SetDataflow(routed(alphaName), vehiclepb.SetDataflowRequest_builder{Toml: []byte(gcsFlow)}.Build())
	if _, err := missionpb.NewMissionServiceClient(h.conn).StartMission(routed(alphaName), &missionpb.StartMissionRequest{}); err != nil {
		t.Fatal(err)
	}
	calls := h.flows.snapshot()
	if len(calls) != 2 {
		t.Fatalf("calls = %d", len(calls))
	}
	f := calls[1].flow
	if len(f.Producers) != 1 || f.Producers[0].Name != alphaName+".telemetry" ||
		len(f.Engines) != 1 || f.Engines[0].Name != "100.64.0.1.mission_sink" ||
		len(f.Edges) != 1 || f.Edges[0].From != alphaName+".telemetry" || f.Edges[0].To != "100.64.0.1.mission_sink" {
		t.Errorf("mission flow = %+v", f)
	}
	g, _ := dc.GetFlow(routed(alphaName), &vehiclepb.GetFlowRequest{})
	parsed, err := config.ParseFlowConfigFromBytes(g.GetToml())
	if err != nil || len(parsed.Engines) != 1 || parsed.Engines[0].Name != "100.64.0.1.mission_sink" {
		t.Errorf("GetFlow = %q, %v", g.GetToml(), err)
	}
}

func TestStopMissionNoMission(t *testing.T) {
	h := newHarness(t)
	mc := missionpb.NewMissionServiceClient(h.conn)
	_, err := mc.StopMission(routed(alphaName), &missionpb.StopMissionRequest{})
	if status.Code(err) != codes.FailedPrecondition || status.Convert(err).Message() != "no mission loaded" {
		t.Errorf("err = %v", err)
	}
	mc.StartMission(routed(alphaName), &missionpb.StartMissionRequest{})
	if _, err := mc.StopMission(routed(alphaName), &missionpb.StopMissionRequest{}); err != nil {
		t.Errorf("stop running mission: %v", err)
	}
}

func TestGetTelemetry(t *testing.T) {
	h := newHarness(t)
	resp, err := vehiclepb.NewDataServiceClient(h.conn).GetTelemetry(routed(bravoName), &vehiclepb.GetTelemetryRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if len(resp.GetTelemetry()) != 1 {
		t.Fatalf("got %d telemetry", len(resp.GetTelemetry()))
	}
	var _ *telemetrypb.Telemetry = resp.GetTelemetry()[0]
	want := h.vehicles[bravoName].Telemetry(time.Now()).GetPositionInfo().GetGlobalPosition().GetLatitude()
	if got := resp.GetTelemetry()[0].GetPositionInfo().GetGlobalPosition().GetLatitude(); got != want {
		t.Errorf("lat = %v, want %v", got, want)
	}
}
