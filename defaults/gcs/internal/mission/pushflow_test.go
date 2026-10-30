package mission

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/flows"
	"github.com/rs/zerolog"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// fakeSetter records the flows set, and fails with err if set.
type fakeSetter struct {
	mu    sync.Mutex
	err   error
	flows map[string][]byte
}

func (f *fakeSetter) SetFlow(_ context.Context, vehicle string, toml []byte) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return f.err
	}
	if f.flows == nil {
		f.flows = map[string][]byte{}
	}
	f.flows[vehicle] = toml
	return nil
}

func pushFlowReq(t testing.TB, tpl *flows.Template) *http.Request {
	t.Helper()
	body, err := json.Marshal(map[string]any{"flow": tpl})
	if err != nil {
		t.Fatal(err)
	}
	return httptest.NewRequest(http.MethodPost, "/api/flows/push", bytes.NewReader(body))
}

// servePush serves req with a FlowPush that lists l and sets flows with s.
func servePush(l Lister, s FlowSetter, req *http.Request) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	NewFlowPush(l, s, testGCSIP, zerolog.Nop()).ServePushFlow(rec, req)
	return rec
}

func TestPushFlowSetsResolvedFlow(t *testing.T) {
	s := &fakeSetter{}
	l := catalogLister(flowCatalog("100.64.0.1"), vehicle("100.64.0.1.alpha", "amd64"), vehicle("100.64.0.1.bravo", "amd64"))
	rec := servePush(l, s, pushFlowReq(t, localYolo()))
	if rec.Code != http.StatusOK {
		t.Fatalf("code %d: %s", rec.Code, rec.Body)
	}
	if len(s.flows) != 1 || s.flows["100.64.0.1.alpha"] == nil {
		t.Fatalf("flows set = %v, want only alpha's", s.flows)
	}
	fc, err := config.ParseFlowConfigFromBytes(s.flows["100.64.0.1.alpha"])
	if err != nil {
		t.Fatal(err)
	}
	if len(fc.Engines) != 1 || fc.Engines[0].Name != "100.64.0.1.yolo" || fc.Engines[0].Location != config.LocationLocal {
		t.Errorf("engines = %+v, want the local yolo", fc.Engines)
	}
}

func TestPushFlowRejectsBeforeSetting(t *testing.T) {
	noVehicle := localYolo()
	noVehicle.Vehicle = ""
	for _, tc := range []struct {
		name string
		tpl  *flows.Template
		code int
		want string
	}{
		{"empty flow", &flows.Template{Vehicle: "100.64.0.1.alpha"}, http.StatusBadRequest, "empty"},
		{"no vehicle", noVehicle, http.StatusBadRequest, "vehicle"},
		{"vehicle not running", localYolo(), http.StatusNotFound, "not running"},
		{"engine not in catalog", remoteDet(), http.StatusUnprocessableEntity, "det"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := &fakeSetter{}
			vs := []fleet.Vehicle{vehicle("100.64.0.1.alpha", "amd64")}
			if tc.name == "vehicle not running" {
				vs = nil
			}
			rec := servePush(catalogLister(flowCatalog("100.64.0.1"), vs...), s, pushFlowReq(t, tc.tpl))
			if rec.Code != tc.code || !strings.Contains(detail(t, rec), tc.want) {
				t.Errorf("code %d detail %q, want %d containing %q", rec.Code, detail(t, rec), tc.code, tc.want)
			}
			if len(s.flows) != 0 {
				t.Errorf("flows set = %v, want none", s.flows)
			}
		})
	}
}

func TestPushFlowDriverErr(t *testing.T) {
	v := vehicle("100.64.0.1.alpha", "amd64")
	v.DriverErr = errors.New("bad toml")
	rec := servePush(catalogLister(flowCatalog("100.64.0.1"), v), &fakeSetter{}, pushFlowReq(t, localYolo()))
	if rec.Code != http.StatusUnprocessableEntity || !strings.Contains(detail(t, rec), "bad toml") {
		t.Errorf("code %d detail %q", rec.Code, detail(t, rec))
	}
}

func TestPushFlowListFailureIs503(t *testing.T) {
	l := func(context.Context) ([]fleet.Vehicle, flows.Catalog, error) {
		return nil, flows.Catalog{}, errors.New("daemon down")
	}
	rec := servePush(l, &fakeSetter{}, pushFlowReq(t, localYolo()))
	if rec.Code != http.StatusServiceUnavailable || !strings.Contains(detail(t, rec), "daemon down") {
		t.Errorf("code %d detail %q", rec.Code, detail(t, rec))
	}
}

func TestPushFlowSetFailureIs502(t *testing.T) {
	s := &fakeSetter{err: status.Error(codes.InvalidArgument, "engine not installed")}
	l := catalogLister(flowCatalog("100.64.0.1"), vehicle("100.64.0.1.alpha", "amd64"))
	rec := servePush(l, s, pushFlowReq(t, localYolo()))
	if rec.Code != http.StatusBadGateway || detail(t, rec) != "Cannot set the vehicle's flow: engine not installed" {
		t.Errorf("code %d detail %q", rec.Code, detail(t, rec))
	}
}
