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
		},
	}.Build()
}

func TestBuild(t *testing.T) {
	got := Build(listResponse(), self)
	want := []Daemon{
		{IP: "100.64.0.1", Hostname: "cloudlet033", Local: true, Arch: "amd64", OS: "linux", Services: []Service{
			{Name: "100.64.0.1.gcs", Bare: "gcs", Plugin: "p-100.64.0.1.gcs", Category: "engine", Running: true, Auth: "admin", Self: true},
		}},
		{IP: "100.64.0.2", Hostname: "swap03", Arch: "arm64", OS: "linux", Services: []Service{
			{Name: "100.64.0.2.charlie", Bare: "charlie", Plugin: "p-100.64.0.2.charlie", Category: "vehicle", Running: true, Auth: "admin"},
			{Name: "100.64.0.2.yolo", Bare: "yolo", Plugin: "p-100.64.0.2.yolo", Category: "engine", Auth: "admin"},
		}},
		{IP: "100.64.0.9", Hostname: "heisenberg", Error: "refused", Services: []Service{}},
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
}

func (f *fakeClient) ListServices(context.Context, *daemonpb.ListServicesRequest, ...grpc.CallOption) (*daemonpb.ListServicesResponse, error) {
	return f.resp, f.listErr
}

func (f *fakeClient) RestartService(_ context.Context, in *daemonpb.RestartServiceRequest, _ ...grpc.CallOption) (*daemonpb.RestartServiceResponse, error) {
	f.restarted = append(f.restarted, in.GetServiceNames())
	return &daemonpb.RestartServiceResponse{}, f.restartErr
}

func TestServeList(t *testing.T) {
	h := NewHandler(&fakeClient{resp: listResponse()}, self, zerolog.Nop())
	rec := httptest.NewRecorder()
	h.ServeList(rec, httptest.NewRequest("GET", "/api/daemons", nil))
	var body struct{ Daemons []map[string]any }
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || rec.Code != http.StatusOK || len(body.Daemons) != 3 {
		t.Fatalf("%d %s (%v)", rec.Code, rec.Body, err)
	}
	// An unavailable daemon has an empty list of services, not null; an
	// available one has no error key
	if svcs, ok := body.Daemons[2]["services"].([]any); !ok || len(svcs) != 0 || body.Daemons[2]["error"] != "refused" {
		t.Errorf("unavailable daemon = %v", body.Daemons[2])
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
