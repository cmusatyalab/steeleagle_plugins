package daemons

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"github.com/rs/zerolog"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	gcsPkg  = "https://github.com/cmusatyalab/steeleagle_plugins//defaults/gcs@v4_backend_rework"
	h264Pkg = "https://github.com/cmusatyalab/steeleagle_plugins//defaults/h264@v4_backend_rework"
)

var modified = time.Date(2026, 10, 28, 18, 39, 30, 0, time.UTC)

func packagesResponse(t *testing.T) *daemonpb.ListPackagesResponse {
	t.Helper()
	gcsTOML, err := os.ReadFile("../../gcs.plugin.toml")
	if err != nil {
		t.Fatal(err)
	}
	encoder := []byte("[plugin]\nname = \"encoder\"\ntype = \"engine\"\n")
	return daemonpb.ListPackagesResponse_builder{Packages: []*daemonpb.Package{
		daemonpb.Package_builder{Uri: h264Pkg, Plugins: map[string][]byte{"encoder": encoder, "decoder": encoder}, Modified: timestamppb.New(modified)}.Build(),
		daemonpb.Package_builder{Uri: gcsPkg, Plugins: map[string][]byte{"gcs": gcsTOML}, Modified: timestamppb.New(modified)}.Build(),
	}}.Build()
}

func TestCatalog(t *testing.T) {
	pkgs := Catalog(packagesResponse(t))
	if len(pkgs) != 2 || pkgs[0].URI != gcsPkg || pkgs[1].URI != h264Pkg {
		t.Fatalf("packages = %+v, want gcs then h264", pkgs)
	}
	if !pkgs[0].Modified.Equal(modified) {
		t.Errorf("modified = %v, want %v", pkgs[0].Modified, modified)
	}
	if names := []string{pkgs[1].Plugins[0].Name, pkgs[1].Plugins[1].Name}; !reflect.DeepEqual(names, []string{"decoder", "encoder"}) {
		t.Errorf("h264 plugins = %v, want sorted", names)
	}
	gcs := pkgs[0].Plugins[0]
	if gcs.Name != "gcs" || gcs.Type != "engine" || gcs.Error != "" || len(gcs.Args) != 6 {
		t.Fatalf("gcs plugin = %+v", gcs)
	}
	var mapbox *Arg
	for i := range gcs.Args {
		if gcs.Args[i].Name == "mapbox" {
			mapbox = &gcs.Args[i]
		}
	}
	if mapbox == nil || mapbox.Type != "string" || mapbox.Default != "" || mapbox.Description == "" {
		t.Errorf("mapbox arg = %+v", mapbox)
	}
	// The web UI reads lowercase keys
	b, _ := json.Marshal(gcs.Args[0])
	if !strings.Contains(string(b), `"name":`) || !strings.Contains(string(b), `"default":`) {
		t.Errorf("arg JSON = %s", b)
	}
}

func TestCatalogBadPluginTOML(t *testing.T) {
	resp := daemonpb.ListPackagesResponse_builder{Packages: []*daemonpb.Package{
		daemonpb.Package_builder{Uri: "u", Plugins: map[string][]byte{"broken": []byte("[plugin")}}.Build(),
	}}.Build()
	p := Catalog(resp)[0].Plugins[0]
	if p.Name != "broken" || p.Error == "" || p.Args != nil {
		t.Errorf("broken plugin = %+v", p)
	}
}

func TestMissingPackages(t *testing.T) {
	pkgs := Catalog(packagesResponse(t))
	svc := func(name, plugin, pkg string) ServiceForm {
		return ServiceForm{Name: name, Type: "engine", Plugin: PluginForm{Plugin: plugin, Package: pkg}}
	}
	cases := map[string]struct {
		form Form
		want []string
	}{
		"installed": {Form{Services: []ServiceForm{svc("gcs", "gcs", gcsPkg)}}, nil},
		"package missing": {Form{Services: []ServiceForm{svc("yolo", "yolo", "github.com/org/yolo")}},
			[]string{`service "yolo" uses package github.com/org/yolo, which isn't installed on this daemon`}},
		"plugin missing": {Form{Services: []ServiceForm{svc("enc", "h264_encoder", h264Pkg)}},
			[]string{`package ` + h264Pkg + ` has no plugin "h264_encoder" (service "enc")`}},
		"simulated service checked too": {Form{Services: []ServiceForm{{Name: "v", Type: "vehicle", Simulate: true, Plugin: PluginForm{Plugin: "sim_vehicle", Package: "github.com/org/sim"}}}},
			[]string{`service "v" uses package github.com/org/sim, which isn't installed on this daemon`}},
		"simulator": {Form{Simulator: &SimulatorForm{Package: "github.com/org/sim"}},
			[]string{`the simulator uses package github.com/org/sim, which isn't installed on this daemon`}},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			if got := MissingPackages(c.form, pkgs); !reflect.DeepEqual(got, c.want) {
				t.Errorf("got %q, want %q", got, c.want)
			}
		})
	}
}

func TestPackageRoutes(t *testing.T) {
	f := &fakeClient{packages: packagesResponse(t)}
	routes := NewHandler(f, self, zerolog.Nop()).Routes()

	rec := httptest.NewRecorder()
	routes["GET /api/daemons/packages"].ServeHTTP(rec, httptest.NewRequest("GET", "/api/daemons/packages?ip=100.64.0.2", nil))
	var list struct{ Packages []Package }
	if err := json.Unmarshal(rec.Body.Bytes(), &list); err != nil || rec.Code != http.StatusOK || len(list.Packages) != 2 {
		t.Fatalf("list: %d %s (%v)", rec.Code, rec.Body, err)
	}

	rec = httptest.NewRecorder()
	routes["POST /api/daemons/packages/install"].ServeHTTP(rec, httptest.NewRequest("POST", "/api/daemons/packages/install", strings.NewReader(`{"ip":"100.64.0.2","uri":"  github.com/org/pkg  "}`)))
	if rec.Code != http.StatusOK || !reflect.DeepEqual(f.installed, []string{"github.com/org/pkg"}) {
		t.Errorf("install: %d %s %v", rec.Code, rec.Body, f.installed)
	}

	rec = httptest.NewRecorder()
	routes["POST /api/daemons/packages/remove"].ServeHTTP(rec, httptest.NewRequest("POST", "/api/daemons/packages/remove", strings.NewReader(`{"ip":"100.64.0.2","uri":"github.com/org/pkg"}`)))
	if rec.Code != http.StatusOK || !reflect.DeepEqual(f.removed, []string{"github.com/org/pkg"}) {
		t.Errorf("remove: %d %s %v", rec.Code, rec.Body, f.removed)
	}

	if !reflect.DeepEqual(f.keys, []string{"100.64.0.2", "100.64.0.2", "100.64.0.2"}) {
		t.Errorf("route keys = %q", f.keys)
	}

	for _, c := range []struct{ path, body string }{
		{"POST /api/daemons/packages/install", `{"ip":"100.64.0.2","uri":"   "}`},
		{"POST /api/daemons/packages/remove", `{"ip":"100.64.0.2"}`},
		{"POST /api/daemons/packages/install", `{"uri":"x"}`},
	} {
		rec = httptest.NewRecorder()
		routes[c.path].ServeHTTP(rec, httptest.NewRequest("POST", strings.TrimPrefix(c.path, "POST "), strings.NewReader(c.body)))
		if rec.Code != http.StatusBadRequest {
			t.Errorf("%s %s: %d", c.path, c.body, rec.Code)
		}
	}
	rec = httptest.NewRecorder()
	routes["GET /api/daemons/packages"].ServeHTTP(rec, httptest.NewRequest("GET", "/api/daemons/packages", nil))
	if rec.Code != http.StatusBadRequest {
		t.Errorf("list without ip: %d", rec.Code)
	}
}

func TestPackageInstallOutlivesTheRequest(t *testing.T) {
	f := &fakeClient{}
	routes := NewHandler(f, self, zerolog.Nop()).Routes()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	for _, path := range []string{"/api/daemons/packages/install", "/api/daemons/packages/remove"} {
		req := httptest.NewRequest("POST", path, strings.NewReader(`{"ip":"100.64.0.2","uri":"u"}`)).WithContext(ctx)
		routes["POST "+path].ServeHTTP(httptest.NewRecorder(), req)
	}
	if !reflect.DeepEqual(f.ctxErrs, []error{nil, nil}) {
		t.Errorf("context errors = %v, want none", f.ctxErrs)
	}
}
