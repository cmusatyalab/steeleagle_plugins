package main

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/control"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/rtc"
	"github.com/rs/zerolog"
	"google.golang.org/grpc"
)

func TestParseArgs(t *testing.T) {
	a, err := parseArgs("")
	if err != nil {
		t.Fatal(err)
	}
	if a.HTTPAddr != ":8002" || a.MaxInputSize != 4<<20 || a.ICEServers != nil || a.WebRTCPorts != "" {
		t.Errorf("parseArgs(\"\") = %+v, want the defaults of gcs.plugin.toml", a)
	}
	// The daemon passes only the args the config sets
	a, err = parseArgs(`{"http_addr": ":9000", "ice_servers": ["stun:example.com"]}`)
	if err != nil {
		t.Fatal(err)
	}
	if a.HTTPAddr != ":9000" || !slices.Equal(a.ICEServers, []string{"stun:example.com"}) || a.MaxInputSize != 4<<20 {
		t.Errorf("parseArgs() = %+v, want the set args over the defaults", a)
	}
	for _, invalid := range []string{`{`, `{"max_input_size": 0}`, `{"max_input_size": "big"}`} {
		if _, err := parseArgs(invalid); err == nil {
			t.Errorf("parseArgs(%s) succeeded, want an error", invalid)
		}
	}
}

func TestParsePorts(t *testing.T) {
	if lo, hi, err := parsePorts(""); err != nil || lo != 0 || hi != 0 {
		t.Errorf("parsePorts(\"\") = %d, %d, %v, want any port", lo, hi, err)
	}
	if lo, hi, err := parsePorts("50000-50100"); err != nil || lo != 50000 || hi != 50100 {
		t.Errorf("parsePorts() = %d, %d, %v, want 50000-50100", lo, hi, err)
	}
	for _, invalid := range []string{"50000", "a-b", "0-10", "200-100", "1-70000"} {
		if _, _, err := parsePorts(invalid); err == nil {
			t.Errorf("parsePorts(%q) succeeded, want an error", invalid)
		}
	}
}

var stubVehicles = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
	io.WriteString(w, "stub vehicles")
})

// TestHandler ensures that the web UI's files are served, with its
// index.html for the paths it routes, and that unknown API paths aren't.
func TestHandler(t *testing.T) {
	web := t.TempDir()
	os.WriteFile(filepath.Join(web, "index.html"), []byte("index"), 0o644)
	os.Mkdir(filepath.Join(web, "assets"), 0o755)
	os.WriteFile(filepath.Join(web, "assets", "app.js"), []byte("app"), 0o644)
	hub, err := rtc.NewHub(rtc.Options{}, zerolog.Nop())
	if err != nil {
		t.Fatal(err)
	}
	defer hub.Close()
	server := httptest.NewServer(newHandler(hub, control.New(nil, control.DefaultTimeout, zerolog.Nop()), stubVehicles, testMissions(), web, zerolog.Nop()))
	defer server.Close()

	for path, want := range map[string]string{
		"/":                    "index",
		"/assets/app.js":       "app",
		"/vehicles/drone1":     "index",
		"/assets":              "index",
		"/../../etc/passwd":    "index",
		"/api/streams":         "[]\n",
		"/api/remote/vehicles": "stub vehicles",
	} {
		resp, err := http.Get(server.URL + path)
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK || string(body) != want {
			t.Errorf("GET %s = %d %q, want %q", path, resp.StatusCode, body, want)
		}
	}
	// Control endpoints are served, and say when there's no daemon
	resp, err := http.Post(server.URL+"/api/command", "application/json", strings.NewReader(`{"land": true, "vehicles": ["a"]}`))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Errorf("POST /api/command = %d, want %d", resp.StatusCode, http.StatusServiceUnavailable)
	}
	resp, err = http.Get(server.URL + "/api/missing")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Errorf("GET /api/missing = %d, want %d", resp.StatusCode, http.StatusNotFound)
	}
}

// TestHandlerWithoutWeb ensures that the API is still served without a
// built web UI.
func TestHandlerWithoutWeb(t *testing.T) {
	hub, err := rtc.NewHub(rtc.Options{}, zerolog.Nop())
	if err != nil {
		t.Fatal(err)
	}
	defer hub.Close()
	server := httptest.NewServer(newHandler(hub, control.New(nil, control.DefaultTimeout, zerolog.Nop()), stubVehicles, testMissions(), filepath.Join(t.TempDir(), "missing"), zerolog.Nop()))
	defer server.Close()
	resp, err := http.Get(server.URL + "/api/streams")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("GET /api/streams = %d, want %d", resp.StatusCode, http.StatusOK)
	}
}

// TestHandlerWithoutDaemon ensures that the vehicles route says when the GCS
// has no connection to the daemon.
func TestHandlerWithoutDaemon(t *testing.T) {
	hub, err := rtc.NewHub(rtc.Options{}, zerolog.Nop())
	if err != nil {
		t.Fatal(err)
	}
	defer hub.Close()
	server := httptest.NewServer(newHandler(hub, control.New(nil, control.DefaultTimeout, zerolog.Nop()), noDaemonHandler(), testMissions(), filepath.Join(t.TempDir(), "missing"), zerolog.Nop()))
	defer server.Close()
	resp, err := http.Get(server.URL + "/api/remote/vehicles")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusServiceUnavailable || !strings.Contains(string(body), `{"detail":"the GCS has no connection to the daemon"}`) {
		t.Errorf("GET /api/remote/vehicles = %d %q, want 503 and the detail", resp.StatusCode, body)
	}
}

// testMissions returns mission routes for a GCS with no daemon and an
// unavailable compiler.
func testMissions() missionRoutes {
	svc := mission.NewService(unavailableCompiler{errors.New("no go")}, mission.CompilerConfig{}, "", nil, zerolog.Nop())
	return missionRoutes{svc: svc, upload: noDaemonHandler(), deploy: noDaemonHandler(), catalog: noDaemonHandler()}
}

// TestParseArgsCompiler ensures that the compiler's config is read from the
// plugin args.
func TestParseArgsCompiler(t *testing.T) {
	a, err := parseArgs(`{"compiler":{"timeout_s":60,"imports":[{"path":"x/y"}]}}`)
	if err != nil {
		t.Fatal(err)
	}
	if a.Compiler.TimeoutS != 60 || len(a.Compiler.Imports) != 1 || a.Compiler.Imports[0].Path != "x/y" {
		t.Errorf("Compiler = %+v, want timeout_s 60 and import x/y", a.Compiler)
	}
}

// TestNoDaemonMissionRoutes ensures that uploading and deploying say when the
// GCS has no connection to the daemon.
func TestNoDaemonMissionRoutes(t *testing.T) {
	hub, err := rtc.NewHub(rtc.Options{}, zerolog.Nop())
	if err != nil {
		t.Fatal(err)
	}
	defer hub.Close()
	server := httptest.NewServer(newHandler(hub, control.New(nil, control.DefaultTimeout, zerolog.Nop()), noDaemonHandler(), testMissions(), filepath.Join(t.TempDir(), "missing"), zerolog.Nop()))
	defer server.Close()
	for _, path := range []string{"/api/upload", "/api/deploy"} {
		resp, err := http.Post(server.URL+path, "application/json", strings.NewReader("{}"))
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != http.StatusServiceUnavailable || !strings.Contains(string(body), "the GCS has no connection to the daemon") {
			t.Errorf("POST %s = %d %q, want 503 and the detail", path, resp.StatusCode, body)
		}
	}
}

// TestCompilerRoutesRegistered ensures that the compiler routes are served
// even when the compiler is unavailable, and say why.
func TestCompilerRoutesRegistered(t *testing.T) {
	hub, err := rtc.NewHub(rtc.Options{}, zerolog.Nop())
	if err != nil {
		t.Fatal(err)
	}
	defer hub.Close()
	server := httptest.NewServer(newHandler(hub, control.New(nil, control.DefaultTimeout, zerolog.Nop()), noDaemonHandler(), testMissions(), filepath.Join(t.TempDir(), "missing"), zerolog.Nop()))
	defer server.Close()
	resp, err := http.Get(server.URL + "/api/schema")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusServiceUnavailable || !strings.Contains(string(body), "FSM compiler unavailable: no go") {
		t.Errorf("GET /api/schema = %d %q, want 503 and the reason", resp.StatusCode, body)
	}
	for _, path := range []string{"/api/compile", "/api/render_dsl", "/api/parse_dsl", "/api/build"} {
		resp, err := http.Post(server.URL+path, "application/json", strings.NewReader("{}"))
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode == http.StatusNotFound {
			t.Errorf("POST %s = 404, want it registered", path)
		}
	}
}

// TestCatalogRoute503WithoutDaemon ensures that the flow catalog route says
// when the GCS has no connection to the daemon.
func TestCatalogRoute503WithoutDaemon(t *testing.T) {
	hub, err := rtc.NewHub(rtc.Options{}, zerolog.Nop())
	if err != nil {
		t.Fatal(err)
	}
	defer hub.Close()
	server := httptest.NewServer(newHandler(hub, control.New(nil, control.DefaultTimeout, zerolog.Nop()), noDaemonHandler(), testMissions(), filepath.Join(t.TempDir(), "missing"), zerolog.Nop()))
	defer server.Close()
	resp, err := http.Get(server.URL + "/api/flows/catalog")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusServiceUnavailable || !strings.Contains(string(body), `{"detail":"the GCS has no connection to the daemon"}`) {
		t.Errorf("GET /api/flows/catalog = %d %q, want 503 and the detail", resp.StatusCode, body)
	}
}

// countingDaemon is a daemon client that counts its ListServices calls.
type countingDaemon struct {
	daemonpb.DaemonServiceClient
	calls int
}

func (d *countingDaemon) ListServices(context.Context, *daemonpb.ListServicesRequest, ...grpc.CallOption) (*daemonpb.ListServicesResponse, error) {
	d.calls++
	return daemonpb.ListServicesResponse_builder{Services: []*daemonpb.Service{
		daemonpb.Service_builder{Name: "100.64.0.1.alpha", Category: daemonpb.ServiceCategory_SERVICE_CATEGORY_VEHICLE, Running: true}.Build(),
		daemonpb.Service_builder{Name: "100.64.0.1.yolo", Category: daemonpb.ServiceCategory_SERVICE_CATEGORY_ENGINE}.Build(),
	}}.Build(), nil
}

// TestDaemonListerListsOnce ensures that the vehicles and the flow catalog
// come from one ListServices call.
func TestDaemonListerListsOnce(t *testing.T) {
	d := &countingDaemon{}
	vs, cat, err := daemonLister(d, "100.64.0.1", "cloudlet033")(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if d.calls != 1 {
		t.Errorf("ListServices called %d times, want 1", d.calls)
	}
	if len(vs) != 1 || vs[0].Name != "100.64.0.1.alpha" {
		t.Errorf("vehicles = %+v", vs)
	}
	if _, ok := cat.Engine("100.64.0.1.yolo"); !ok {
		t.Errorf("catalog = %+v", cat)
	}
}

// configDaemon is a daemon client whose GetConfiguration answers with toml or
// err.
type configDaemon struct {
	daemonpb.DaemonServiceClient
	toml string
	err  error
}

func (d configDaemon) GetConfiguration(context.Context, *daemonpb.GetConfigurationRequest, ...grpc.CallOption) (*daemonpb.GetConfigurationResponse, error) {
	return daemonpb.GetConfigurationResponse_builder{ConfigToml: []byte(d.toml)}.Build(), d.err
}

func TestDaemonHostname(t *testing.T) {
	osHost, _ := os.Hostname()
	tests := []struct {
		name string
		d    configDaemon
		want string
	}{
		{"configured", configDaemon{toml: "[daemon]\nhostname = \"cloudlet033\"\n"}, "cloudlet033"},
		{"default", configDaemon{toml: "[daemon]\nport = 5000\n"}, "eagled"},
		{"rpc fails", configDaemon{err: errors.New("unimplemented")}, osHost},
		{"bad toml", configDaemon{toml: "[daemon"}, osHost},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := daemonHostname(context.Background(), tc.d, zerolog.Nop()); got != tc.want {
				t.Errorf("daemonHostname() = %q, want %q", got, tc.want)
			}
		})
	}
}
