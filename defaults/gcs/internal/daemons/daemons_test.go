package daemons

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"github.com/rs/zerolog"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

var self = Self{IP: "100.64.0.1", Hostname: "cloudlet033", Arch: "amd64", OS: "linux", Engine: "100.64.0.1.gcs"}

func svc(name string, c daemonpb.ServiceCategory, running bool) *daemonpb.Service {
	return daemonpb.Service_builder{Name: name, Category: c, Plugin: "p-" + name, Running: running, Auth: "admin"}.Build()
}

func listResponse() *daemonpb.ListServicesResponse {
	return daemonpb.ListServicesResponse_builder{
		Services: []*daemonpb.Service{
			svc("100.64.0.2.yolo", daemonpb.ServiceCategory_SERVICE_CATEGORY_ENGINE, false),
			svc("100.64.0.2.charlie", daemonpb.ServiceCategory_SERVICE_CATEGORY_VEHICLE, true),
			svc("100.64.0.1.gcs", daemonpb.ServiceCategory_SERVICE_CATEGORY_ENGINE, true),
			svc("unqualified", daemonpb.ServiceCategory_SERVICE_CATEGORY_ENGINE, true),
		},
		Remotes: []*daemonpb.Daemon{
			daemonpb.Daemon_builder{Ip: "100.64.0.2", Hostname: "swap03", Arch: "arm64", Os: "linux"}.Build(),
			daemonpb.Daemon_builder{Ip: "100.64.0.9", Hostname: "heisenberg", Error: "refused"}.Build(),
			daemonpb.Daemon_builder{Ip: "100.64.0.8", Hostname: "steppe", Error: "rpc error: code = FailedPrecondition desc = daemon failed to start services: no package x"}.Build(),
		},
	}.Build()
}

func TestBuild(t *testing.T) {
	got := Build(listResponse(), self)
	want := []Daemon{
		{IP: "100.64.0.1", Hostname: "cloudlet033", Local: true, Arch: "amd64", OS: "linux", State: "up", Services: []Service{
			{Name: "100.64.0.1.gcs", Bare: "gcs", Plugin: "p-100.64.0.1.gcs", Category: "engine", Running: true, Auth: "admin", Self: true},
		}},
		{IP: "100.64.0.2", Hostname: "swap03", Arch: "arm64", OS: "linux", State: "up", Services: []Service{
			{Name: "100.64.0.2.charlie", Bare: "charlie", Plugin: "p-100.64.0.2.charlie", Category: "vehicle", Running: true, Auth: "admin"},
			{Name: "100.64.0.2.yolo", Bare: "yolo", Plugin: "p-100.64.0.2.yolo", Category: "engine", Auth: "admin"},
		}},
		{IP: "100.64.0.8", Hostname: "steppe", State: "services_failed", Error: "rpc error: code = FailedPrecondition desc = daemon failed to start services: no package x", Services: []Service{}},
		{IP: "100.64.0.9", Hostname: "heisenberg", State: "unreachable", Error: "refused", Services: []Service{}},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("Build =\n%+v\nwant\n%+v", got, want)
	}
}

func TestBuildOwnDaemonWithNoServices(t *testing.T) {
	got := Build(&daemonpb.ListServicesResponse{}, self)
	if len(got) != 1 || !got[0].Local || got[0].Services == nil {
		t.Errorf("Build = %+v", got)
	}
}

type fakeClient struct {
	resp       *daemonpb.ListServicesResponse
	listErr    error
	restartErr error
	restarted  []string

	// err is what the config, package and Restart calls return
	err        error
	keys       []string // the x-route-key of each of those calls
	ctxErrs    []error  // their contexts' errors when called
	config     []byte
	configured []string
	restarts   int
	packages   *daemonpb.ListPackagesResponse
	installed  []string
	removed    []string
}

func (f *fakeClient) note(ctx context.Context) {
	md, _ := metadata.FromOutgoingContext(ctx)
	f.keys = append(f.keys, strings.Join(md.Get("x-route-key"), ","))
	f.ctxErrs = append(f.ctxErrs, ctx.Err())
}

func (f *fakeClient) ListServices(context.Context, *daemonpb.ListServicesRequest, ...grpc.CallOption) (*daemonpb.ListServicesResponse, error) {
	return f.resp, f.listErr
}

func (f *fakeClient) RestartService(_ context.Context, in *daemonpb.RestartServiceRequest, _ ...grpc.CallOption) (*daemonpb.RestartServiceResponse, error) {
	f.restarted = append(f.restarted, in.GetServiceNames())
	return &daemonpb.RestartServiceResponse{}, f.restartErr
}

func (f *fakeClient) GetConfiguration(ctx context.Context, _ *daemonpb.GetConfigurationRequest, _ ...grpc.CallOption) (*daemonpb.GetConfigurationResponse, error) {
	f.note(ctx)
	if f.err != nil {
		return nil, f.err
	}
	return daemonpb.GetConfigurationResponse_builder{ConfigToml: f.config}.Build(), nil
}

func (f *fakeClient) Configure(ctx context.Context, in *daemonpb.ConfigureRequest, _ ...grpc.CallOption) (*daemonpb.ConfigureResponse, error) {
	f.note(ctx)
	if f.err != nil {
		return nil, f.err
	}
	f.configured = append(f.configured, in.GetConfigToml())
	return &daemonpb.ConfigureResponse{}, nil
}

func (f *fakeClient) Restart(ctx context.Context, _ *daemonpb.RestartRequest, _ ...grpc.CallOption) (*daemonpb.RestartResponse, error) {
	f.note(ctx)
	if f.err != nil {
		return nil, f.err
	}
	f.restarts++
	return &daemonpb.RestartResponse{}, nil
}

func (f *fakeClient) ListPackages(ctx context.Context, _ *daemonpb.ListPackagesRequest, _ ...grpc.CallOption) (*daemonpb.ListPackagesResponse, error) {
	f.note(ctx)
	if f.err != nil {
		return nil, f.err
	}
	if f.packages == nil {
		return &daemonpb.ListPackagesResponse{}, nil
	}
	return f.packages, nil
}

func (f *fakeClient) InstallPackage(ctx context.Context, in *daemonpb.InstallPackageRequest, _ ...grpc.CallOption) (*daemonpb.InstallPackageResponse, error) {
	f.note(ctx)
	if f.err != nil {
		return nil, f.err
	}
	f.installed = append(f.installed, in.GetUri())
	return &daemonpb.InstallPackageResponse{}, nil
}

func (f *fakeClient) RemovePackage(ctx context.Context, in *daemonpb.RemovePackageRequest, _ ...grpc.CallOption) (*daemonpb.RemovePackageResponse, error) {
	f.note(ctx)
	if f.err != nil {
		return nil, f.err
	}
	f.removed = append(f.removed, in.GetUri())
	return &daemonpb.RemovePackageResponse{}, nil
}

func TestServeList(t *testing.T) {
	h := NewHandler(&fakeClient{resp: listResponse()}, self, zerolog.Nop())
	rec := httptest.NewRecorder()
	h.ServeList(rec, httptest.NewRequest("GET", "/api/daemons", nil))
	var body struct{ Daemons []map[string]any }
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || rec.Code != http.StatusOK || len(body.Daemons) != 4 {
		t.Fatalf("%d %s (%v)", rec.Code, rec.Body, err)
	}
	// An unavailable daemon has an empty list of services, not null; an
	// available one has no error key
	if svcs, ok := body.Daemons[3]["services"].([]any); !ok || len(svcs) != 0 || body.Daemons[3]["error"] != "refused" {
		t.Errorf("unavailable daemon = %v", body.Daemons[3])
	}
	if _, ok := body.Daemons[0]["error"]; ok {
		t.Errorf("available daemon has an error: %v", body.Daemons[0])
	}

	h = NewHandler(&fakeClient{listErr: errors.New("down")}, self, zerolog.Nop())
	rec = httptest.NewRecorder()
	h.ServeList(rec, httptest.NewRequest("GET", "/api/daemons", nil))
	if rec.Code != http.StatusBadGateway || !strings.Contains(rec.Body.String(), "down") {
		t.Errorf("%d %s", rec.Code, rec.Body)
	}
}

func TestServeRestart(t *testing.T) {
	f := &fakeClient{}
	h := NewHandler(f, self, zerolog.Nop())
	rec := httptest.NewRecorder()
	h.ServeRestart(rec, httptest.NewRequest("POST", "/api/daemons/restart_service", strings.NewReader(`{"name":"100.64.0.2.yolo"}`)))
	if rec.Code != http.StatusOK || !reflect.DeepEqual(f.restarted, []string{"100.64.0.2.yolo"}) {
		t.Errorf("%d %s %v", rec.Code, rec.Body, f.restarted)
	}

	for _, body := range []string{`{}`, `not json`} {
		rec = httptest.NewRecorder()
		h.ServeRestart(rec, httptest.NewRequest("POST", "/api/daemons/restart_service", strings.NewReader(body)))
		if rec.Code != http.StatusBadRequest {
			t.Errorf("%s: %d %s", body, rec.Code, rec.Body)
		}
	}

	f.restartErr = status.Error(codes.NotFound, `no service "x"`)
	rec = httptest.NewRecorder()
	h.ServeRestart(rec, httptest.NewRequest("POST", "/api/daemons/restart_service", strings.NewReader(`{"name":"x"}`)))
	if rec.Code != http.StatusBadGateway || strings.TrimSpace(rec.Body.String()) != `{"detail":"no service \"x\""}` {
		t.Errorf("%d %s", rec.Code, rec.Body)
	}
}

func TestServeRestartOutlivesTheRequest(t *testing.T) {
	f := &ctxClient{}
	h := NewHandler(f, self, zerolog.Nop())
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	req := httptest.NewRequest("POST", "/api/daemons/restart_service", strings.NewReader(`{"name":"100.64.0.2.yolo"}`)).WithContext(ctx)
	h.ServeRestart(httptest.NewRecorder(), req)
	if f.err != nil {
		t.Errorf("restart saw the request's cancellation: %v", f.err)
	}
}

// ctxClient records the context error its RestartService was called with.
type ctxClient struct {
	fakeClient
	err error
}

func (c *ctxClient) RestartService(ctx context.Context, _ *daemonpb.RestartServiceRequest, _ ...grpc.CallOption) (*daemonpb.RestartServiceResponse, error) {
	c.err = ctx.Err()
	return &daemonpb.RestartServiceResponse{}, nil
}

func TestRestartDaemonRoutesByIP(t *testing.T) {
	f := &fakeClient{}
	h := NewHandler(f, self, zerolog.Nop())
	routes := h.Routes()
	rec := httptest.NewRecorder()
	routes["POST /api/daemons/restart"].ServeHTTP(rec, httptest.NewRequest("POST", "/api/daemons/restart", strings.NewReader(`{"ip":"100.64.0.2"}`)))
	if rec.Code != http.StatusOK || strings.TrimSpace(rec.Body.String()) != `{"ok":true}` || f.restarts != 1 {
		t.Fatalf("%d %s, restarts %d", rec.Code, rec.Body, f.restarts)
	}
	if !reflect.DeepEqual(f.keys, []string{"100.64.0.2"}) {
		t.Errorf("route keys = %q, want [100.64.0.2]", f.keys)
	}
	rec = httptest.NewRecorder()
	routes["POST /api/daemons/restart"].ServeHTTP(rec, httptest.NewRequest("POST", "/api/daemons/restart", strings.NewReader(`{}`)))
	if rec.Code != http.StatusBadRequest {
		t.Errorf("no ip: %d %s", rec.Code, rec.Body)
	}
}

func TestRestartDaemonOutlivesTheRequest(t *testing.T) {
	f := &fakeClient{}
	h := NewHandler(f, self, zerolog.Nop())
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	req := httptest.NewRequest("POST", "/api/daemons/restart", strings.NewReader(`{"ip":"100.64.0.2"}`)).WithContext(ctx)
	h.Routes()["POST /api/daemons/restart"].ServeHTTP(httptest.NewRecorder(), req)
	if len(f.ctxErrs) != 1 || f.ctxErrs[0] != nil {
		t.Errorf("restart saw the request's cancellation: %v", f.ctxErrs)
	}
}

func TestRPCErrorMapping(t *testing.T) {
	cases := []struct {
		code codes.Code
		want int
	}{
		{codes.PermissionDenied, http.StatusForbidden},
		{codes.NotFound, http.StatusNotFound},
		{codes.InvalidArgument, http.StatusUnprocessableEntity},
		{codes.FailedPrecondition, http.StatusConflict},
		{codes.Unavailable, http.StatusBadGateway},
		{codes.Internal, http.StatusBadGateway},
	}
	for _, c := range cases {
		rec := httptest.NewRecorder()
		writeRPCError(rec, status.Error(c.code, "the reason"))
		var body struct{ Detail string }
		if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
			t.Fatal(err)
		}
		if rec.Code != c.want {
			t.Errorf("%s: %d, want %d", c.code, rec.Code, c.want)
		}
		if strings.Contains(body.Detail, "rpc error") || !strings.HasPrefix(body.Detail, "the reason") {
			t.Errorf("%s: detail %q", c.code, body.Detail)
		}
		wantSuffix := c.code == codes.PermissionDenied
		if got := strings.HasSuffix(body.Detail, ` (the GCS service needs auth = "admin" in this daemon's config)`); got != wantSuffix {
			t.Errorf("%s: detail %q, auth hint %v, want %v", c.code, body.Detail, got, wantSuffix)
		}
	}
}

func TestRoutesCoverPatterns(t *testing.T) {
	routes := NewHandler(&fakeClient{}, self, zerolog.Nop()).Routes()
	if len(routes) != len(Patterns) {
		t.Errorf("%d routes, %d patterns", len(routes), len(Patterns))
	}
	for _, p := range Patterns {
		if routes[p] == nil {
			t.Errorf("no route for %q", p)
		}
	}
}
