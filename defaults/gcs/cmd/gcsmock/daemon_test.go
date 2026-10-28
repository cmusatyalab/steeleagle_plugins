package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
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
	var forms []string
	for _, e := range f.Edges {
		forms = append(forms, e.From)
	}
	if !slices.Contains(forms, alphaName+".front") || !slices.Contains(forms, alphaName+".telemetry") {
		t.Errorf("edges not renamed: %v", forms)
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

func upload(t *testing.T, h *harness, vehicle, name string, chunks ...string) error {
	t.Helper()
	stream, err := vehiclepb.NewLoaderServiceClient(h.conn).UploadMission(routed(vehicle))
	if err != nil {
		t.Fatal(err)
	}
	for i, c := range chunks {
		err := stream.Send(vehiclepb.UploadMissionRequest_builder{
			Filename: name, Chunk: []byte(c), Number: int32(i + 1), Total: int32(len(chunks)),
		}.Build())
		if err != nil {
			t.Fatal(err)
		}
		ack, err := stream.Recv()
		if err != nil {
			return err
		}
		if ack.GetReceived() != int32(i+1) {
			t.Fatalf("ack %d, want %d", ack.GetReceived(), i+1)
		}
	}
	return stream.CloseSend()
}

func getMissions(t *testing.T, h *harness, vehicle string) *vehiclepb.GetMissionsResponse {
	t.Helper()
	resp, err := vehiclepb.NewLoaderServiceClient(h.conn).GetMissions(routed(vehicle), &vehiclepb.GetMissionsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	return resp
}

func TestLoaderUploadThenGetMissions(t *testing.T) {
	h := newHarness(t)
	if err := upload(t, h, alphaName, "m1", "he", "llo"); err != nil {
		t.Fatal(err)
	}
	resp := getMissions(t, h, alphaName)
	if len(resp.GetMissions()) != 1 {
		t.Fatalf("missions = %v", resp.GetMissions())
	}
	m := resp.GetMissions()[0]
	sum := sha256.Sum256([]byte("hello"))
	if m.GetName() != "m1" || m.GetHash() != hex.EncodeToString(sum[:]) || m.GetCreationTime().AsTime().IsZero() {
		t.Errorf("mission = %v", m)
	}
	if resp.HasActiveMission() {
		t.Error("nothing is loaded yet")
	}
	if got := getMissions(t, h, bravoName).GetMissions(); len(got) != 0 {
		t.Errorf("bravo has %v", got)
	}
}

func TestLoaderUploadReplacesSameName(t *testing.T) {
	h := newHarness(t)
	for _, c := range []string{"one", "two"} {
		if err := upload(t, h, alphaName, "m1", c); err != nil {
			t.Fatal(err)
		}
	}
	ms := getMissions(t, h, alphaName).GetMissions()
	sum := sha256.Sum256([]byte("two"))
	if len(ms) != 1 || ms[0].GetHash() != hex.EncodeToString(sum[:]) {
		t.Errorf("missions = %v", ms)
	}
}

func TestLoaderLoadSetsActive(t *testing.T) {
	h := newHarness(t)
	upload(t, h, alphaName, "m1", "x")
	if _, err := vehiclepb.NewLoaderServiceClient(h.conn).LoadMission(routed(alphaName),
		vehiclepb.LoadMissionRequest_builder{Name: "m1"}.Build()); err != nil {
		t.Fatal(err)
	}
	if a := getMissions(t, h, alphaName).GetActiveMission(); a.GetName() != "m1" {
		t.Errorf("active = %v", a)
	}
}

func TestLoaderLoadRefusedWhileMissionRunning(t *testing.T) {
	h := newHarness(t)
	upload(t, h, alphaName, "m1", "x")
	if _, err := missionpb.NewMissionServiceClient(h.conn).StartMission(routed(alphaName), &missionpb.StartMissionRequest{}); err != nil {
		t.Fatal(err)
	}
	_, err := vehiclepb.NewLoaderServiceClient(h.conn).LoadMission(routed(alphaName),
		vehiclepb.LoadMissionRequest_builder{Name: "m1"}.Build())
	if status.Code(err) != codes.FailedPrecondition || status.Convert(err).Message() != "cannot load a mission while one is running" {
		t.Errorf("err = %v", err)
	}
}

func TestLoaderLoadMissingIsNotFound(t *testing.T) {
	h := newHarness(t)
	_, err := vehiclepb.NewLoaderServiceClient(h.conn).LoadMission(routed(alphaName),
		vehiclepb.LoadMissionRequest_builder{Name: "nope"}.Build())
	if status.Code(err) != codes.NotFound {
		t.Errorf("err = %v", err)
	}
	_, err = vehiclepb.NewLoaderServiceClient(h.conn).LoadMission(routed(alphaName),
		vehiclepb.LoadMissionRequest_builder{Name: "../x"}.Build())
	if status.Code(err) != codes.InvalidArgument {
		t.Errorf("bad name: err = %v", err)
	}
}

func TestLoaderRejectsBadChunkNumbering(t *testing.T) {
	h := newHarness(t)
	stream, err := vehiclepb.NewLoaderServiceClient(h.conn).UploadMission(routed(alphaName))
	if err != nil {
		t.Fatal(err)
	}
	stream.Send(vehiclepb.UploadMissionRequest_builder{Filename: "m1", Chunk: []byte("a"), Number: 2, Total: 2}.Build())
	_, err = stream.Recv()
	if status.Code(err) != codes.InvalidArgument {
		t.Errorf("err = %v", err)
	}
	if got := getMissions(t, h, alphaName).GetMissions(); len(got) != 0 {
		t.Errorf("a failed transfer left %v", got)
	}
}

func TestListServicesRemotes(t *testing.T) {
	h := newHarness(t)
	resp, err := daemonpb.NewDaemonServiceClient(h.conn).ListServices(context.Background(), &daemonpb.ListServicesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]*daemonpb.Daemon{}
	for _, r := range resp.GetRemotes() {
		got[r.GetIp()] = r
	}
	// The harness's GCS is on 100.64.0.9, which has no vehicles
	if len(got) != 3 {
		t.Fatalf("got remotes %v, want the 3 vehicle daemons", resp.GetRemotes())
	}
	for _, ip := range []string{"100.64.0.1", "100.64.0.2", "100.64.0.3"} {
		r := got[ip]
		if r == nil || r.GetArch() != "amd64" || r.GetOs() != "linux" || r.GetHostname() != "mock-"+ip {
			t.Errorf("remote %s = %v", ip, r)
		}
	}
}

func TestListServicesRemotesArchAndGCSExcluded(t *testing.T) {
	top := Topology{
		GCSIP:    "100.64.0.1",
		Vehicles: []string{alphaName, bravoName, charlieName},
		Archs:    map[string]string{"100.64.0.3": "arm64"},
	}
	d := NewDaemon(top, nil, &recordingFlows{}, zerolog.Nop())
	resp, err := d.ListServices(context.Background(), &daemonpb.ListServicesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]string{}
	for _, r := range resp.GetRemotes() {
		got[r.GetIp()] = r.GetArch()
	}
	want := map[string]string{"100.64.0.2": "amd64", "100.64.0.3": "arm64"}
	if len(got) != len(want) || got["100.64.0.2"] != "amd64" || got["100.64.0.3"] != "arm64" {
		t.Errorf("remotes = %v, want %v (the GCS's own daemon is never listed)", got, want)
	}
}

func TestListServicesEnginesHaveConfigs(t *testing.T) {
	o, err := parseFlags([]string{"-vehicles", alphaName + "," + charlieName, "-no-yolo-daemons", "100.64.0.3", "-gcs-ip", "100.64.0.9"})
	if err != nil {
		t.Fatal(err)
	}
	d := NewDaemon(o.topology(), nil, &recordingFlows{}, zerolog.Nop())
	resp, err := d.ListServices(context.Background(), &daemonpb.ListServicesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	engines := map[string]*config.EngineCap{}
	for _, s := range resp.GetServices() {
		if s.GetCategory() != daemonpb.ServiceCategory_SERVICE_CATEGORY_ENGINE {
			continue
		}
		pc, err := config.ParsePluginConfigFromBytes(s.GetConfig())
		if err != nil {
			t.Errorf("engine %s: %v", s.GetName(), err)
			continue
		}
		if pc.Engine == nil {
			t.Errorf("engine %s has no engine table", s.GetName())
			continue
		}
		engines[s.GetName()] = pc.Engine
	}
	const (
		raw     = "steeleagle_protocol/v1/messages/frame/RawFrame"
		encoded = "steeleagle_protocol/v1/messages/frame/EncodedFrame"
		tele    = "steeleagle_protocol/v1/messages/telemetry/Telemetry"
	)
	for _, ip := range []string{"100.64.0.1", "100.64.0.3"} {
		e := engines[ip+".h264_encoder"]
		// A filter, as the real H.264 encoder's plugin file declares it
		if e == nil || e.Type != "filter" || !slices.Equal(e.Inputs, []string{raw}) || e.Result != "" {
			t.Errorf("%s encoder = %+v", ip, e)
		}
	}
	if e := engines["100.64.0.9.gcs"]; e == nil || !slices.Equal(e.Inputs, []string{encoded, tele}) || e.Result != "" {
		t.Errorf("gcs = %+v", e)
	}
	if e := engines["100.64.0.1.yolo"]; e == nil || e.Type != "detection" || !slices.Equal(e.Inputs, []string{raw}) ||
		e.Result != "steeleagle_protocol/v1/messages/result/DetectionResult" {
		t.Errorf("yolo = %+v", e)
	}
	if engines["100.64.0.3.yolo"] != nil {
		t.Error("-no-yolo-daemons left yolo on 100.64.0.3")
	}
}

func TestRestartService(t *testing.T) {
	d := NewDaemon(Topology{GCSIP: "100.64.0.9"}, nil, nil, zerolog.Nop())
	ctx := context.Background()
	if _, err := d.RestartService(ctx, daemonpb.RestartServiceRequest_builder{ServiceNames: "100.64.0.9.gcs"}.Build()); err != nil {
		t.Errorf("restart gcs: %v", err)
	}
	if _, err := d.RestartService(ctx, daemonpb.RestartServiceRequest_builder{ServiceNames: "nope"}.Build()); status.Code(err) != codes.NotFound {
		t.Errorf("restart nope: %v", err)
	}
}
