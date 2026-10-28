package control

import (
	"bytes"
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"slices"
	"sync"
	"testing"
	"time"

	driverpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/driver"
	missionpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/mission"
	"github.com/rs/zerolog"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
)

// received is a call the fake daemon received.
type received struct {
	vehicle string // its route key
	method  string
	req     proto.Message
}

// fakeDaemon stands in for the daemon routing calls to vehicles. Calls to the
// vehicles in fail fail, and calls to the vehicles in block wait until their
// context is done.
type fakeDaemon struct {
	driverpb.UnimplementedControlServiceServer
	missionpb.UnimplementedMissionServiceServer
	fail  map[string]bool
	block map[string]bool
	// noMission makes StopMission fail as it does for a vehicle with no mission
	// loaded.
	noMission map[string]bool
	// hold overrides the result of Hold per vehicle.
	hold map[string]codes.Code
	// delay is how long Hold waits before answering.
	delay time.Duration

	mu    sync.Mutex
	calls []received
}

func (d *fakeDaemon) record(ctx context.Context, method string, req proto.Message) error {
	md, _ := metadata.FromIncomingContext(ctx)
	keys := md.Get(routeHeader)
	if len(keys) != 1 {
		return status.Errorf(codes.InvalidArgument, "got route keys %v, want one", keys)
	}
	d.mu.Lock()
	d.calls = append(d.calls, received{keys[0], method, req})
	d.mu.Unlock()
	if d.block[keys[0]] {
		<-ctx.Done()
		return status.FromContextError(ctx.Err()).Err()
	}
	if d.fail[keys[0]] {
		return status.Errorf(codes.PermissionDenied, "%s is not allowed", method)
	}
	return nil
}

// received returns the calls received so far, sorted by vehicle and stably
// in the order each vehicle received them.
func (d *fakeDaemon) received() []received {
	d.mu.Lock()
	defer d.mu.Unlock()
	calls := slices.Clone(d.calls)
	slices.SortStableFunc(calls, func(a, b received) int {
		if a.vehicle < b.vehicle {
			return -1
		} else if a.vehicle > b.vehicle {
			return 1
		}
		return 0
	})
	return calls
}

func (d *fakeDaemon) TakeOff(ctx context.Context, req *driverpb.TakeOffRequest) (*driverpb.TakeOffResponse, error) {
	return &driverpb.TakeOffResponse{}, d.record(ctx, "TakeOff", req)
}

func (d *fakeDaemon) Land(ctx context.Context, req *driverpb.LandRequest) (*driverpb.LandResponse, error) {
	return &driverpb.LandResponse{}, d.record(ctx, "Land", req)
}

func (d *fakeDaemon) ReturnToHome(ctx context.Context, req *driverpb.ReturnToHomeRequest) (*driverpb.ReturnToHomeResponse, error) {
	return &driverpb.ReturnToHomeResponse{}, d.record(ctx, "ReturnToHome", req)
}

func (d *fakeDaemon) Hold(ctx context.Context, req *driverpb.HoldRequest) (*driverpb.HoldResponse, error) {
	err := d.record(ctx, "Hold", req)
	time.Sleep(d.delay)
	if md, _ := metadata.FromIncomingContext(ctx); err == nil && len(md.Get(routeHeader)) == 1 {
		if code, ok := d.hold[md.Get(routeHeader)[0]]; ok {
			err = status.Errorf(code, "Hold failed")
		}
	}
	return &driverpb.HoldResponse{}, err
}

func (d *fakeDaemon) SetVelocityTarget(ctx context.Context, req *driverpb.SetVelocityTargetRequest) (*driverpb.SetVelocityTargetResponse, error) {
	return &driverpb.SetVelocityTargetResponse{}, d.record(ctx, "SetVelocityTarget", req)
}

func (d *fakeDaemon) SetGimbalAngleTarget(ctx context.Context, req *driverpb.SetGimbalAngleTargetRequest) (*driverpb.SetGimbalAngleTargetResponse, error) {
	return &driverpb.SetGimbalAngleTargetResponse{}, d.record(ctx, "SetGimbalAngleTarget", req)
}

// startCount is how many StartMission calls the daemon got.
func (d *fakeDaemon) startCount() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	n := 0
	for _, c := range d.calls {
		if c.method == "StartMission" {
			n++
		}
	}
	return n
}

func (d *fakeDaemon) StartMission(ctx context.Context, req *missionpb.StartMissionRequest) (*missionpb.StartMissionResponse, error) {
	return &missionpb.StartMissionResponse{}, d.record(ctx, "StartMission", req)
}

func (d *fakeDaemon) StopMission(ctx context.Context, req *missionpb.StopMissionRequest) (*missionpb.StopMissionResponse, error) {
	err := d.record(ctx, "StopMission", req)
	if md, _ := metadata.FromIncomingContext(ctx); err == nil && len(md.Get(routeHeader)) == 1 && d.noMission[md.Get(routeHeader)[0]] {
		err = status.Error(codes.FailedPrecondition, "no mission loaded")
	}
	return &missionpb.StopMissionResponse{}, err
}

// serve serves the fake daemon d, and returns a server for a controller
// calling it with timeout.
func serve(t *testing.T, d *fakeDaemon, timeout time.Duration) *httptest.Server {
	t.Helper()
	server, _ := serveWith(t, d, timeout)
	return server
}

// serveWith is serve, and also returns the controller.
func serveWith(t *testing.T, d *fakeDaemon, timeout time.Duration) (*httptest.Server, *Controller) {
	t.Helper()
	ln := bufconn.Listen(1 << 20)
	server := grpc.NewServer()
	driverpb.RegisterControlServiceServer(server, d)
	missionpb.RegisterMissionServiceServer(server, d)
	go server.Serve(ln)
	t.Cleanup(server.Stop)
	conn, err := grpc.NewClient("passthrough:///daemon",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return ln.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	c := New(conn, timeout, zerolog.Nop())
	return serveController(t, c), c
}

func serveController(t *testing.T, c *Controller) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/start", c.ServeStart)
	mux.HandleFunc("POST /api/command", c.ServeCommand)
	mux.HandleFunc("POST /api/joystick", c.ServeJoystick)
	mux.HandleFunc("POST /api/gimbal", c.ServeGimbal)
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	return server
}

// post posts body as JSON to path, returning the status and the decoded
// response.
func post(t *testing.T, server *httptest.Server, path, body string) (int, Response, string) {
	t.Helper()
	resp, err := http.Post(server.URL+path, "application/json", bytes.NewBufferString(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var raw json.RawMessage
	if err := json.NewDecoder(resp.Body).Decode(&raw); err != nil {
		t.Fatalf("POST %s: response isn't JSON: %v", path, err)
	}
	var out Response
	var detail struct{ Detail string }
	json.Unmarshal(raw, &out)
	json.Unmarshal(raw, &detail)
	return resp.StatusCode, out, detail.Detail
}

// methods returns the vehicle and method of each call.
func methods(calls []received) [][2]string {
	var out [][2]string
	for _, c := range calls {
		out = append(out, [2]string{c.vehicle, c.method})
	}
	return out
}

// TestCommands ensures that each endpoint makes its calls on every vehicle,
// routed to the vehicle, with the request's values.
func TestCommands(t *testing.T) {
	for _, tc := range []struct {
		path, body string
		calls      []string // per vehicle, in order
		check      func(t *testing.T, req proto.Message)
	}{
		{"/api/start", `{"vehicles": ["a", "b"]}`, []string{"StartMission"}, nil},
		{"/api/command", `{"takeoff": 12, "vehicles": ["a", "b"]}`, []string{"TakeOff"},
			func(t *testing.T, req proto.Message) {
				if got := req.(*driverpb.TakeOffRequest).GetAltitude(); got != 12 {
					t.Errorf("take off altitude = %v, want 12", got)
				}
			}},
		{"/api/command", `{"land": true, "vehicles": ["a", "b"]}`, []string{"Land"}, nil},
		{"/api/command", `{"rth": true, "vehicles": ["a", "b"]}`, []string{"ReturnToHome"}, nil},
		{"/api/command", `{"hold": true, "vehicles": ["a", "b"]}`, []string{"Hold", "StopMission"}, nil},
		{"/api/command", `{"stop_mission": true, "vehicles": ["a", "b"]}`, []string{"StopMission"}, nil},
		{"/api/joystick", `{"xvel": 1, "yvel": -2, "zvel": 0.5, "angularvel": 30, "duration": 1, "vehicles": ["a", "b"]}`,
			[]string{"SetVelocityTarget"}, func(t *testing.T, req proto.Message) {
				v := req.(*driverpb.SetVelocityTargetRequest).GetVelocity()
				if v.GetXVel() != 1 || v.GetYVel() != -2 || v.GetZVel() != 0.5 || v.GetAngularVel() != 30 {
					t.Errorf("velocity = %v, want the joystick's", v)
				}
				if !v.HasZVel() {
					t.Error("velocity leaves out z, want every axis set")
				}
			}},
		{"/api/gimbal", `{"pitch": 5, "yaw": -3, "vehicles": ["a", "b"]}`, []string{"SetGimbalAngleTarget"},
			func(t *testing.T, req proto.Message) {
				r := req.(*driverpb.SetGimbalAngleTargetRequest)
				if p := r.GetPose(); p.GetPitch() != 5 || p.GetYaw() != -3 || p.GetRoll() != 0 {
					t.Errorf("pose = %v, want the request's", p)
				}
				if r.GetAngleMode() != driverpb.AngleMode_ANGLE_MODE_OFFSET {
					t.Errorf("angle mode = %v, want an offset", r.GetAngleMode())
				}
			}},
	} {
		t.Run(tc.path+" "+tc.body, func(t *testing.T) {
			d := &fakeDaemon{}
			server := serve(t, d, DefaultTimeout)
			code, resp, detail := post(t, server, tc.path, tc.body)
			if code != http.StatusOK {
				t.Fatalf("status %d (%s), want %d", code, detail, http.StatusOK)
			}
			var want [][2]string
			var wantResults []Result
			for _, vehicle := range []string{"a", "b"} {
				for _, m := range tc.calls {
					want = append(want, [2]string{vehicle, m})
					wantResults = append(wantResults, Result{Vehicle: vehicle, Command: m, Success: true})
				}
			}
			calls := d.received()
			if got := methods(calls); !slices.Equal(got, want) {
				t.Errorf("daemon received %v, want %v", got, want)
			}
			if !slices.Equal(resp.Results, wantResults) {
				t.Errorf("results = %+v, want %+v", resp.Results, wantResults)
			}
			if tc.check != nil {
				for _, c := range calls {
					tc.check(t, c.req)
				}
			}
		})
	}
}

// TestCommandFailures ensures that a vehicle whose call fails is reported
// without failing the others, and that a hold still stops the mission of a
// vehicle that can't hold.
func TestCommandFailures(t *testing.T) {
	d := &fakeDaemon{fail: map[string]bool{"b": true}}
	server := serve(t, d, DefaultTimeout)
	code, resp, _ := post(t, server, "/api/command", `{"hold": true, "vehicles": ["a", "b"]}`)
	if code != http.StatusOK {
		t.Fatalf("status %d, want %d", code, http.StatusOK)
	}
	want := []Result{
		{Vehicle: "a", Command: "Hold", Success: true},
		{Vehicle: "a", Command: "StopMission", Success: true},
		{Vehicle: "b", Command: "Hold", Details: "Hold is not allowed"},
		{Vehicle: "b", Command: "StopMission", Details: "StopMission is not allowed"},
	}
	if !slices.Equal(resp.Results, want) {
		t.Errorf("results = %+v, want %+v", resp.Results, want)
	}
}

// TestCommandsConcurrent ensures that a vehicle that doesn't answer doesn't
// hold up the others, and times out.
func TestCommandsConcurrent(t *testing.T) {
	d := &fakeDaemon{block: map[string]bool{"slow": true}}
	server := serve(t, d, 300*time.Millisecond)
	start := time.Now()
	code, resp, _ := post(t, server, "/api/command", `{"land": true, "vehicles": ["slow", "a", "b"]}`)
	if code != http.StatusOK {
		t.Fatalf("status %d, want %d", code, http.StatusOK)
	}
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Errorf("took %v, want about one timeout", elapsed)
	}
	if len(resp.Results) != 3 || resp.Results[0].Vehicle != "slow" || resp.Results[0].Success {
		t.Fatalf("results = %+v, want slow to time out first", resp.Results)
	}
	for _, r := range resp.Results[1:] {
		if !r.Success {
			t.Errorf("result %+v, want success", r)
		}
	}
}

// TestJoystickUnreachableVehicle ensures that a vehicle that doesn't answer
// holds up manual flight of the others for no more than JoystickTimeout, even
// with a long controller timeout.
func TestJoystickUnreachableVehicle(t *testing.T) {
	d := &fakeDaemon{block: map[string]bool{"a": true}}
	server := serve(t, d, 10*time.Second)
	for _, tc := range []struct{ path, body, command string }{
		{"/api/joystick", `{"xvel": 1, "vehicles": ["a", "b"]}`, "SetVelocityTarget"},
		{"/api/gimbal", `{"pitch": 1, "vehicles": ["a", "b"]}`, "SetGimbalAngleTarget"},
	} {
		start := time.Now()
		code, resp, _ := post(t, server, tc.path, tc.body)
		if code != http.StatusOK {
			t.Fatalf("%s: status %d, want %d", tc.path, code, http.StatusOK)
		}
		if elapsed := time.Since(start); elapsed > 1500*time.Millisecond {
			t.Errorf("%s: took %v, want under 1.5s", tc.path, elapsed)
		}
		healthy := Result{Vehicle: "b", Command: tc.command, Success: true}
		if len(resp.Results) != 2 || resp.Results[0].Vehicle != "a" || resp.Results[0].Success ||
			resp.Results[1] != healthy {
			t.Errorf("%s: results = %+v, want a to time out and %+v", tc.path, resp.Results, healthy)
		}
	}
}

func TestInvalidRequests(t *testing.T) {
	d := &fakeDaemon{}
	server := serve(t, d, DefaultTimeout)
	for _, tc := range []struct{ path, body string }{
		{"/api/start", `{"vehicles": []}`},
		{"/api/start", `{}`},
		{"/api/start", `{"vehicles": [""]}`},
		{"/api/start", `not json`},
		{"/api/command", `{"vehicles": ["a"]}`},
		{"/api/command", `{"land": true, "rth": true, "vehicles": ["a"]}`},
		{"/api/command", `{"takeoff": -1, "vehicles": ["a"]}`},
		{"/api/command", `{"arm": true, "vehicles": ["a"]}`},
		{"/api/joystick", `{"xvel": "fast", "vehicles": ["a"]}`},
	} {
		code, _, detail := post(t, server, tc.path, tc.body)
		if code != http.StatusBadRequest || detail == "" {
			t.Errorf("POST %s %s = %d %q, want %d with a detail", tc.path, tc.body, code, detail, http.StatusBadRequest)
		}
	}
	if calls := d.received(); len(calls) != 0 {
		t.Errorf("daemon received %v for invalid requests, want nothing", methods(calls))
	}
}

// TestDuplicateVehicles ensures that a vehicle named twice is sent its
// command once.
func TestDuplicateVehicles(t *testing.T) {
	d := &fakeDaemon{}
	server := serve(t, d, DefaultTimeout)
	if code, resp, _ := post(t, server, "/api/command", `{"land": true, "vehicles": ["a", "a"]}`); code != http.StatusOK || len(resp.Results) != 1 {
		t.Errorf("got %d %+v, want one result", code, resp.Results)
	}
	if calls := d.received(); len(calls) != 1 {
		t.Errorf("daemon received %v, want one call", methods(calls))
	}
}

// TestFalseCommand ensures that a command set to false sends nothing.
func TestFalseCommand(t *testing.T) {
	d := &fakeDaemon{}
	server := serve(t, d, DefaultTimeout)
	if code, resp, _ := post(t, server, "/api/command", `{"land": false, "vehicles": ["a"]}`); code != http.StatusOK || len(resp.Results) != 0 {
		t.Errorf("got %d %+v, want no results", code, resp.Results)
	}
	if calls := d.received(); len(calls) != 0 {
		t.Errorf("daemon received %v, want nothing", methods(calls))
	}
}

// TestWithoutDaemon ensures that a GCS without a daemon connection says so.
func TestWithoutDaemon(t *testing.T) {
	server := serveController(t, New(nil, DefaultTimeout, zerolog.Nop()))
	if code, _, detail := post(t, server, "/api/start", `{"vehicles": ["a"]}`); code != http.StatusServiceUnavailable || detail == "" {
		t.Errorf("got %d %q, want %d with a detail", code, detail, http.StatusServiceUnavailable)
	}
}

// hookRecorder records the vehicles the hold hook was called with.
type hookRecorder struct {
	mu    sync.Mutex
	calls [][]string
}

func (h *hookRecorder) hook(vehicles []string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.calls = append(h.calls, slices.Clone(vehicles))
}

// TestHoldNoMissionIsSuccess ensures that a vehicle with no mission to stop
// is reported as stopped.
func TestHoldNoMissionIsSuccess(t *testing.T) {
	d := &fakeDaemon{noMission: map[string]bool{"a": true}}
	server := serve(t, d, DefaultTimeout)
	_, resp, _ := post(t, server, "/api/command", `{"hold": true, "vehicles": ["a"]}`)
	want := []Result{
		{Vehicle: "a", Command: "Hold", Success: true},
		{Vehicle: "a", Command: "StopMission", Success: true},
	}
	if !slices.Equal(resp.Results, want) {
		t.Errorf("results = %+v, want %+v", resp.Results, want)
	}
	_, resp, _ = post(t, server, "/api/command", `{"stop_mission": true, "vehicles": ["a"]}`)
	if len(resp.Results) != 1 || !resp.Results[0].Success || resp.Results[0].Details != "" {
		t.Errorf("stop_mission results = %+v, want success", resp.Results)
	}
}

// TestHoldHook ensures that the hook gets the vehicles whose Hold didn't fail
// for lack of an answer.
func TestHoldHook(t *testing.T) {
	d := &fakeDaemon{hold: map[string]codes.Code{"b": codes.Unavailable, "c": codes.PermissionDenied}}
	server, c := serveWith(t, d, DefaultTimeout)
	var h hookRecorder
	c.OnHold(h.hook)
	post(t, server, "/api/command", `{"hold": true, "vehicles": ["a", "b", "c"]}`)
	if want := [][]string{{"a", "c"}}; !slices.EqualFunc(h.calls, want, slices.Equal) {
		t.Errorf("hook calls = %v, want %v", h.calls, want)
	}
}

// TestStartHook ensures that the start hook gets every vehicle a start is
// requested for, before any mission is started.
func TestStartHook(t *testing.T) {
	d := &fakeDaemon{}
	server, c := serveWith(t, d, DefaultTimeout)
	var h hookRecorder
	c.OnStart(func(vehicles []string) {
		if n := d.startCount(); n != 0 {
			t.Errorf("hook called after %d StartMission calls, want before any", n)
		}
		h.hook(vehicles)
	})
	post(t, server, "/api/start", `{"vehicles": ["a", "b"]}`)
	if want := [][]string{{"a", "b"}}; !slices.EqualFunc(h.calls, want, slices.Equal) {
		t.Errorf("hook calls = %v, want %v", h.calls, want)
	}
}

// TestHoldHookNotCalledForStop ensures that stopping a mission doesn't call
// the hold hook.
func TestHoldHookNotCalledForStop(t *testing.T) {
	server, c := serveWith(t, &fakeDaemon{}, DefaultTimeout)
	var h hookRecorder
	c.OnHold(h.hook)
	post(t, server, "/api/command", `{"stop_mission": true, "vehicles": ["a"]}`)
	if len(h.calls) != 0 {
		t.Errorf("hook calls = %v, want none", h.calls)
	}
}

// TestHoldUnreachableIsFast ensures that a vehicle that doesn't answer holds
// up a hold by its hold timeouts, not the controller's.
func TestHoldUnreachableIsFast(t *testing.T) {
	d := &fakeDaemon{block: map[string]bool{"b": true}}
	server, c := serveWith(t, d, 10*time.Second)
	var h hookRecorder
	c.OnHold(h.hook)
	start := time.Now()
	_, resp, _ := post(t, server, "/api/command", `{"hold": true, "vehicles": ["a", "b"]}`)
	if elapsed := time.Since(start); elapsed > 7500*time.Millisecond {
		t.Errorf("took %v, want within two hold timeouts", elapsed)
	}
	var found bool
	for _, r := range resp.Results {
		if r.Vehicle == "b" && r.Command == "Hold" {
			found = true
			if r.Success {
				t.Errorf("b's Hold = %+v, want a failure", r)
			}
		}
	}
	if !found {
		t.Errorf("results = %+v, want b's Hold", resp.Results)
	}
	if want := [][]string{{"a"}}; !slices.EqualFunc(h.calls, want, slices.Equal) {
		t.Errorf("hook calls = %v, want %v", h.calls, want)
	}
}

// TestHoldSurvivesDisconnect ensures that a browser going away mid-hold
// doesn't cancel the hold, skip stopping the mission, or skip the hook.
func TestHoldSurvivesDisconnect(t *testing.T) {
	d := &fakeDaemon{delay: 300 * time.Millisecond}
	server, c := serveWith(t, d, DefaultTimeout)
	var h hookRecorder
	c.OnHold(h.hook)
	ctx, cancel := context.WithCancel(context.Background())
	req, _ := http.NewRequestWithContext(ctx, "POST", server.URL+"/api/command",
		bytes.NewBufferString(`{"hold": true, "vehicles": ["a"]}`))
	time.AfterFunc(100*time.Millisecond, cancel)
	if resp, err := http.DefaultClient.Do(req); err == nil {
		resp.Body.Close()
		t.Fatal("request succeeded, want it canceled")
	}
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		h.mu.Lock()
		n := len(h.calls)
		h.mu.Unlock()
		if n > 0 {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if got, want := methods(d.received()), [][2]string{{"a", "Hold"}, {"a", "StopMission"}}; !slices.Equal(got, want) {
		t.Errorf("daemon received %v, want %v", got, want)
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if want := [][]string{{"a"}}; !slices.EqualFunc(h.calls, want, slices.Equal) {
		t.Errorf("hook calls = %v, want %v", h.calls, want)
	}
}
