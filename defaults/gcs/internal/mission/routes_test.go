package mission

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"sync"
	"testing"

	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/flows"
	"github.com/rs/zerolog"
)

// fakeCompiler is a Compiler double, like the Python backend's FakeEaglecClient.
type fakeCompiler struct {
	mu          sync.Mutex
	registry    map[string]any
	registryErr error
	buildErr    func(BuildRequest) error
	binaries    map[string][]byte
	registryN   int
	plugins     [][]byte // the plugin of each Registry call
	builds      []BuildRequest
}

func (f *fakeCompiler) Registry(_ context.Context, plugin []byte) (map[string]any, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.registryN++
	f.plugins = append(f.plugins, plugin)
	return f.registry, f.registryErr
}

func (f *fakeCompiler) Build(_ context.Context, req BuildRequest) ([]byte, error) {
	f.mu.Lock()
	f.builds = append(f.builds, req)
	f.mu.Unlock()
	if f.buildErr != nil {
		if err := f.buildErr(req); err != nil {
			return nil, err
		}
	}
	if b, ok := f.binaries[req.Arch]; ok {
		return b, nil
	}
	return []byte("ELF-" + req.Arch), nil
}

func testConfig(t testing.TB) CompilerConfig {
	t.Helper()
	cfg, err := ResolveConfig(CompilerConfig{SDKVersion: "v0.0.0-test"})
	if err != nil {
		t.Fatal(err)
	}
	return cfg
}

func newFake(t testing.TB) *fakeCompiler {
	t.Helper()
	raw, err := os.ReadFile("testdata/registry.json")
	if err != nil {
		t.Fatal(err)
	}
	return &fakeCompiler{registry: jsonMap(t, string(raw))}
}

// testGCSIP is the IP of the GCS's own daemon, the orchestrator, in tests.
const testGCSIP = "100.64.0.1"

func newService(t testing.TB, f *fakeCompiler) *Service {
	t.Helper()
	return NewService(f, testConfig(t), testGCSIP, nil, nil, zerolog.Nop())
}

// newFlowService returns a Service whose daemon lists cat.
func newFlowService(t testing.TB, f *fakeCompiler, cat flows.Catalog) *Service {
	t.Helper()
	return NewService(f, testConfig(t), testGCSIP, func(context.Context) (flows.Catalog, error) { return cat, nil }, nil, zerolog.Nop())
}

func graphBody(t testing.TB, mutate func(map[string]any)) []byte {
	t.Helper()
	b, err := json.Marshal(exGraph(t))
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatal(err)
	}
	if mutate != nil {
		mutate(m)
	}
	out, _ := json.Marshal(m)
	return out
}

func do(h http.HandlerFunc, method, target string, body []byte) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	h(rec, httptest.NewRequest(method, target, bytes.NewReader(body)))
	return rec
}

func decodeMap(t testing.TB, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &m); err != nil {
		t.Fatalf("body %q: %v", rec.Body.String(), err)
	}
	return m
}

func TestSchemaIsTranslatedAndListsImports(t *testing.T) {
	s := newService(t, newFake(t))
	rec := do(s.ServeSchema, "GET", "/api/schema", nil)
	if rec.Code != 200 {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	m := decodeMap(t, rec)
	patrol := m["actions"].(map[string]any)["Patrol"].(map[string]any)
	if patrol["type"] != "actions.Patrol" {
		t.Errorf("Patrol type = %v", patrol["type"])
	}
	first := m["imports"].([]any)[0].(map[string]any)
	if !strings.HasSuffix(first["path"].(string), "/sdk/dsl/actions") {
		t.Errorf("first import = %v", first)
	}
	if _, ok := m["default_role"]; ok {
		t.Error("schema has default_role")
	}
}

func TestSchemaRefreshReloads(t *testing.T) {
	f := newFake(t)
	s := newService(t, f)
	if rec := do(s.ServeSchema, "GET", "/api/schema?refresh=1", nil); rec.Code != 200 {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	if f.registryN != 2 {
		t.Errorf("registry loads = %d, want 2", f.registryN)
	}
}

// newVehicleService returns a Service whose daemon runs vs.
func newVehicleService(t testing.TB, f *fakeCompiler, vs ...fleet.Vehicle) *Service {
	t.Helper()
	return NewService(f, testConfig(t), testGCSIP, nil,
		func(context.Context) ([]fleet.Vehicle, error) { return vs, nil }, zerolog.Nop())
}

var harpy = fleet.Vehicle{Name: "100.64.0.2.harpy", Driver: &config.DriverCap{Type: "copter"}, Config: []byte("[driver]\ntype=\"copter\"\n")}

func TestSchemaForVehicleLoadsWithItsPluginOnce(t *testing.T) {
	f := newFake(t)
	s := newVehicleService(t, f, harpy)
	for range 2 {
		if rec := do(s.ServeSchema, "GET", "/api/schema?vehicle=100.64.0.2.harpy", nil); rec.Code != 200 {
			t.Fatalf("status %d: %s", rec.Code, rec.Body)
		}
	}
	if len(f.plugins) != 2 || f.plugins[0] != nil || string(f.plugins[1]) != string(harpy.Config) {
		t.Errorf("registry plugins = %q, want the default load then harpy's config once", f.plugins)
	}
}

func TestSchemaRefreshDropsVehicleSchemas(t *testing.T) {
	f := newFake(t)
	s := newVehicleService(t, f, harpy)
	for _, url := range []string{"/api/schema?vehicle=100.64.0.2.harpy", "/api/schema?refresh=1&vehicle=100.64.0.2.harpy"} {
		if rec := do(s.ServeSchema, "GET", url, nil); rec.Code != 200 {
			t.Fatalf("%s: status %d: %s", url, rec.Code, rec.Body)
		}
	}
	if f.registryN != 4 {
		t.Errorf("registry loads = %d, want 4", f.registryN)
	}
}

func TestSchemaForVehicleErrors(t *testing.T) {
	broken := fleet.Vehicle{Name: "100.64.0.2.broken", DriverErr: errors.New("plugin config has no driver cap")}
	tests := []struct {
		name    string
		s       func(*fakeCompiler) *Service
		vehicle string
		code    int
		detail  string
	}{
		{"no daemon", func(f *fakeCompiler) *Service { return newService(t, f) }, harpy.Name, 503, noDaemon},
		{"not running", func(f *fakeCompiler) *Service { return newVehicleService(t, f, harpy) }, "100.64.0.2.gone", 404, "vehicle 100.64.0.2.gone is not running"},
		{"no driver", func(f *fakeCompiler) *Service { return newVehicleService(t, f, broken) }, broken.Name, 422,
			"vehicle 100.64.0.2.broken has no driver config: plugin config has no driver cap"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			rec := do(tt.s(newFake(t)).ServeSchema, "GET", "/api/schema?vehicle="+tt.vehicle, nil)
			if rec.Code != tt.code {
				t.Fatalf("status %d, want %d: %s", rec.Code, tt.code, rec.Body)
			}
			if got := decodeMap(t, rec)["detail"]; got != tt.detail {
				t.Errorf("detail = %v, want %v", got, tt.detail)
			}
		})
	}
}

func TestCompileAndBuildUseTheVehiclesPlugin(t *testing.T) {
	f := newFake(t)
	s := newVehicleService(t, f, harpy)
	setVehicle := func(m map[string]any) { m["vehicle"] = harpy.Name; m["arch"] = "arm64" }
	if rec := do(s.ServeCompile, "POST", "/api/compile", graphBody(t, setVehicle)); rec.Code != 200 {
		t.Fatalf("compile status %d: %s", rec.Code, rec.Body)
	}
	if rec := do(s.ServeBuild, "POST", "/api/build", graphBody(t, setVehicle)); rec.Code != 200 {
		t.Fatalf("build status %d: %s", rec.Code, rec.Body)
	}
	if len(f.builds) != 2 {
		t.Fatalf("builds = %d, want 2", len(f.builds))
	}
	for _, b := range f.builds {
		if string(b.Plugin) != string(harpy.Config) {
			t.Errorf("%s build plugin = %q, want harpy's config", b.Arch, b.Plugin)
		}
	}
}

func TestSetupFailureIs503(t *testing.T) {
	f := newFake(t)
	f.registryErr = errors.New("building eaglec needs the go toolchain on PATH")
	s := newService(t, f)
	rec := do(s.ServeSchema, "GET", "/api/schema", nil)
	if rec.Code != 503 {
		t.Fatalf("status %d", rec.Code)
	}
	want := "FSM compiler unavailable: building eaglec needs the go toolchain on PATH"
	if got := decodeMap(t, rec)["detail"]; got != want {
		t.Errorf("detail = %v, want %v", got, want)
	}
	if rec := do(s.ServeCompile, "POST", "/api/compile", graphBody(t, nil)); rec.Code != 503 {
		t.Errorf("compile status %d", rec.Code)
	}
}

func TestCompileOkCompilesAmd64WithoutGeoJSON(t *testing.T) {
	f := newFake(t)
	s := newService(t, f)
	rec := do(s.ServeCompile, "POST", "/api/compile", graphBody(t, nil))
	if rec.Code != 200 || !reflect.DeepEqual(decodeMap(t, rec), map[string]any{"ok": true}) {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	if len(f.builds) != 1 {
		t.Fatalf("builds = %d", len(f.builds))
	}
	b := f.builds[0]
	want := exAST(t)
	if !reflect.DeepEqual(b.Ast["Actions"], want["Actions"]) || !reflect.DeepEqual(b.Ast["Mission"], want["Mission"]) {
		t.Errorf("ast = %v", b.Ast)
	}
	if b.GeoJSON != nil || b.Arch != "amd64" || b.Plugin != nil {
		t.Errorf("request = geojson %q arch %q plugin %q", b.GeoJSON, b.Arch, b.Plugin)
	}
}

func TestCompilePassesDrawnFeatures(t *testing.T) {
	f := newFake(t)
	s := newService(t, f)
	body := graphBody(t, func(m map[string]any) { m["geojson"] = exPolyGeoJSON })
	if rec := do(s.ServeCompile, "POST", "/api/compile", body); rec.Code != 200 {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	if got := string(f.builds[0].GeoJSON); got != exPolyGeoJSON {
		t.Errorf("geojson = %q", got)
	}
}

func TestCompileIgnoresUnknownFields(t *testing.T) {
	s := newService(t, newFake(t))
	body := graphBody(t, func(m map[string]any) { m["frontend_extra"] = 1 })
	if rec := do(s.ServeCompile, "POST", "/api/compile", body); rec.Code != 200 {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
}

func TestCompileStructuralErrorsSkipCompiler(t *testing.T) {
	f := newFake(t)
	s := newService(t, f)
	body := graphBody(t, func(m map[string]any) { m["start_id"] = nil })
	rec := do(s.ServeCompile, "POST", "/api/compile", body)
	want := `{"errors":[{"node_id":null,"event_id":null,"message":"No start state: right-click a node and set it as the start state"}]}`
	if rec.Code != 200 || strings.TrimSpace(rec.Body.String()) != want {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	if len(f.builds) != 0 {
		t.Error("compiler was called")
	}
}

func TestCompileFeatureNameErrorsSkipCompiler(t *testing.T) {
	f := newFake(t)
	s := newService(t, f)
	bad := strings.Replace(exPolyGeoJSON, `"Poly"`, `"Area B"`, 1)
	rec := do(s.ServeCompile, "POST", "/api/compile", graphBody(t, func(m map[string]any) { m["geojson"] = bad }))
	want := `{"errors":[{"node_id":null,"event_id":null,"message":"Map feature 'Area B' can only contain letters, digits and underscores"}]}`
	if strings.TrimSpace(rec.Body.String()) != want {
		t.Fatalf("body %s", rec.Body)
	}
	if len(f.builds) != 0 {
		t.Error("compiler was called")
	}
}

func TestCompileErrorsMapDeclsToNodesAndEvents(t *testing.T) {
	f := newFake(t)
	f.buildErr = func(BuildRequest) error {
		return &CompileErrors{Errors: []DeclError{
			{Decl: "patrol", Message: "no field Altitud"},
			{Decl: "low", Message: "bad comparator"},
			{Message: "go build failed"},
		}}
	}
	s := newService(t, f)
	rec := do(s.ServeCompile, "POST", "/api/compile", graphBody(t, nil))
	want := `{"errors":[{"node_id":"patrol","event_id":null,"message":"no field Altitud"},` +
		`{"node_id":null,"event_id":"low","message":"bad comparator"},` +
		`{"node_id":null,"event_id":null,"message":"go build failed"}]}`
	if rec.Code != 200 || strings.TrimSpace(rec.Body.String()) != want {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
}

func TestCompilerFailureIs500(t *testing.T) {
	f := newFake(t)
	f.buildErr = func(BuildRequest) error { return errors.New("eaglec exited with status 2\nusage: ...") }
	s := newService(t, f)
	rec := do(s.ServeCompile, "POST", "/api/compile", graphBody(t, nil))
	if rec.Code != 500 {
		t.Fatalf("status %d", rec.Code)
	}
	d := decodeMap(t, rec)["detail"].(string)
	if !strings.Contains(d, "status 2") || !strings.Contains(d, "usage: ...") {
		t.Errorf("detail = %q", d)
	}
}

func TestCompileRejectsNonIdentifierNamesAndReservedDone(t *testing.T) {
	f := newFake(t)
	s := newService(t, f)
	body := graphBody(t, func(m map[string]any) {
		m["nodes"].([]any)[0].(map[string]any)["instance_id"] = "land here"
		m["events"].([]any)[0].(map[string]any)["instance_id"] = "done"
	})
	rec := do(s.ServeCompile, "POST", "/api/compile", body)
	out := rec.Body.String()
	for _, want := range []string{
		`"node_id":"land here","event_id":null,"message":"Node name 'land here' can only contain letters, digits and underscores, and can't start with a digit"`,
		`"node_id":null,"event_id":"done","message":"Event name 'done' is reserved for the action-finished transition"`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %s in %s", want, out)
		}
	}
	if len(f.builds) != 0 {
		t.Error("compiler was called")
	}
}

func TestRenderDSL(t *testing.T) {
	s := newService(t, newFake(t))
	rec := do(s.ServeRenderDSL, "POST", "/api/render_dsl", graphBody(t, nil))
	if rec.Code != 200 {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	dsl := decodeMap(t, rec)["dsl"].(string)
	for _, want := range []string{
		"    actions.Patrol patrol(Area=params.Polygon_Poly, Altitude=15.0, Pattern=actions.PatrolModeSurvey)\n",
		"    Start takeoff\n",
	} {
		if !strings.Contains(dsl, want) {
			t.Errorf("dsl lacks %q:\n%s", want, dsl)
		}
	}
	if strings.Contains(dsl, "Role") {
		t.Error("dsl has a Role")
	}
}

func TestRenderDSLWithoutStart(t *testing.T) {
	s := newService(t, newFake(t))
	rec := do(s.ServeRenderDSL, "POST", "/api/render_dsl", graphBody(t, func(m map[string]any) { m["start_id"] = nil }))
	if rec.Code != 200 {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	if dsl := decodeMap(t, rec)["dsl"].(string); strings.Contains(dsl, "Mission:") {
		t.Errorf("dsl has a Mission:\n%s", dsl)
	}
}

func TestParseDSLReturnsGraphAndWarnsAboutExtraImports(t *testing.T) {
	s := newService(t, newFake(t))
	dsl := strings.Replace(exDSL, "Actions:\n", "    \"example.com/x/actions\"\n\nActions:\n", 1)
	body, _ := json.Marshal(map[string]string{"dsl": dsl})
	rec := do(s.ServeParseDSL, "POST", "/api/parse_dsl", body)
	if rec.Code != 200 {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	m := decodeMap(t, rec)
	if m["start_id"] != "takeoff" {
		t.Errorf("start_id = %v", m["start_id"])
	}
	if len(m["nodes"].([]any)) != 4 {
		t.Errorf("nodes = %v", m["nodes"])
	}
	want := []any{"Imports not in the GCS configuration are ignored when compiling: example.com/x/actions"}
	if !reflect.DeepEqual(m["warnings"], want) {
		t.Errorf("warnings = %v", m["warnings"])
	}
}

func TestParseDSLUpgradesTheLegacyImportHeader(t *testing.T) {
	s := newService(t, newFake(t))
	legacy, err := os.ReadFile("testdata/legacy_import.dsl")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := json.Marshal(map[string]string{"dsl": string(legacy)})
	rec := do(s.ServeParseDSL, "POST", "/api/parse_dsl", body)
	if rec.Code != 200 {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	w := decodeMap(t, rec)["warnings"].([]any)
	if len(w) == 0 || w[0] != "Converted the old Import: header to Imports:" {
		t.Errorf("warnings = %v", w)
	}
}

func TestParseDSLSyntaxErrorIs422(t *testing.T) {
	s := newService(t, newFake(t))
	body, _ := json.Marshal(map[string]string{"dsl": "Actions:\n  x"})
	rec := do(s.ServeParseDSL, "POST", "/api/parse_dsl", body)
	if rec.Code != 422 {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	if d := decodeMap(t, rec)["detail"].(string); d == "" {
		t.Error("empty detail")
	}
}

func TestBuildReturnsTheBinary(t *testing.T) {
	f := newFake(t)
	f.binaries = map[string][]byte{"arm64": []byte("\x7fELF-arm")}
	s := newService(t, f)
	rec := do(s.ServeBuild, "POST", "/api/build", graphBody(t, func(m map[string]any) { m["arch"] = "arm64" }))
	if rec.Code != 200 || rec.Body.String() != "\x7fELF-arm" {
		t.Fatalf("status %d: %q", rec.Code, rec.Body)
	}
	if ct := rec.Header().Get("Content-Type"); ct != "application/octet-stream" {
		t.Errorf("content type %q", ct)
	}
	if cd := rec.Header().Get("Content-Disposition"); cd != `attachment; filename="mission-arm64"` {
		t.Errorf("content disposition %q", cd)
	}
	if len(f.builds) != 1 || f.builds[0].Arch != "arm64" {
		t.Errorf("builds = %v", f.builds)
	}
}

func TestBuildCompileErrorsAre422(t *testing.T) {
	f := newFake(t)
	f.buildErr = func(BuildRequest) error {
		return &CompileErrors{Errors: []DeclError{{Decl: "patrol", Message: "bad area"}}}
	}
	s := newService(t, f)
	rec := do(s.ServeBuild, "POST", "/api/build", graphBody(t, func(m map[string]any) { m["arch"] = "amd64" }))
	if rec.Code != 422 {
		t.Fatalf("status %d", rec.Code)
	}
	if d := decodeMap(t, rec)["detail"]; d != "Build failed for amd64: bad area" {
		t.Errorf("detail = %v", d)
	}
}

func TestBuildRejectsUnknownArch(t *testing.T) {
	s := newService(t, newFake(t))
	rec := do(s.ServeBuild, "POST", "/api/build", graphBody(t, func(m map[string]any) { m["arch"] = "riscv" }))
	if rec.Code != 422 {
		t.Fatalf("status %d", rec.Code)
	}
}

func TestCompileGraphPrefixesGroupErrors(t *testing.T) {
	f := newFake(t)
	f.buildErr = func(req BuildRequest) error {
		errs := []DeclError{{Message: "Y"}}
		if req.Arch == "amd64" {
			errs = append([]DeclError{{Message: "X"}}, errs...)
		}
		return &CompileErrors{Errors: errs}
	}
	s := newService(t, f)
	_, errs, err := s.CompileGraph(context.Background(), exGraph(t), []BuildTarget{
		{Key: "alpha, bravo", Arch: "amd64"},
		{Key: "charlie", Arch: "arm64"},
	})
	if err != nil {
		t.Fatal(err)
	}
	var msgs []string
	for _, e := range errs {
		msgs = append(msgs, e.Message)
	}
	if want := []string{"alpha, bravo: X", "Y"}; !reflect.DeepEqual(msgs, want) {
		t.Errorf("messages = %q, want %q", msgs, want)
	}
}

func TestCompileGraphSingleTargetNeverPrefixes(t *testing.T) {
	f := newFake(t)
	f.buildErr = func(BuildRequest) error {
		return &CompileErrors{Errors: []DeclError{{Decl: "patrol", Message: "X"}}}
	}
	s := newService(t, f)
	_, errs, _ := s.CompileGraph(context.Background(), exGraph(t), []BuildTarget{{Key: "alpha", Arch: "amd64"}})
	if len(errs) != 1 || errs[0].Message != "X" || errs[0].NodeID == nil || *errs[0].NodeID != "patrol" {
		t.Errorf("errs = %+v", errs)
	}
}

func TestCompileGraphKeysByTarget(t *testing.T) {
	f := newFake(t)
	s := newService(t, f)
	bins, errs, err := s.CompileGraph(context.Background(), exGraph(t), []BuildTarget{
		{Key: "one", Arch: "amd64", Plugin: []byte("p1")},
		{Key: "two", Arch: "arm64"},
		{Key: "three", Arch: "amd64"},
	})
	if err != nil || len(errs) != 0 {
		t.Fatalf("err %v errs %v", err, errs)
	}
	want := map[string][]byte{"one": []byte("ELF-amd64"), "two": []byte("ELF-arm64"), "three": []byte("ELF-amd64")}
	if !reflect.DeepEqual(bins, want) {
		t.Errorf("bins = %q", bins)
	}
	plugins := map[string]string{}
	for _, b := range f.builds {
		plugins[b.Arch+string(b.Plugin)] = "x"
	}
	if plugins["amd64p1"] == "" {
		t.Error("plugin was not passed to its target's build")
	}
}

func TestCompileGraphCompilerFailureIsAnError(t *testing.T) {
	f := newFake(t)
	f.buildErr = func(BuildRequest) error { return errors.New("timed out after 300 s") }
	s := newService(t, f)
	_, errs, err := s.CompileGraph(context.Background(), exGraph(t), []BuildTarget{{Key: "a", Arch: "amd64"}})
	if err == nil || len(errs) != 0 || err.Error() != "timed out after 300 s" {
		t.Errorf("err = %v, errs = %v", err, errs)
	}
}

func TestCompileWithFlow(t *testing.T) {
	f := engineFake(t)
	s := newFlowService(t, f, flowCatalog("100.64.0.1", "100.64.0.2"))
	rec := do(s.ServeCompile, "POST", "/api/compile", trackBody(t, "100.64.0.1.yolo", localYolo(), nil))
	if rec.Code != 200 || !reflect.DeepEqual(decodeMap(t, rec), map[string]any{"ok": true}) {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	if len(f.builds) != 1 {
		t.Fatalf("builds = %d", len(f.builds))
	}
	b := f.builds[0]
	if b.Arch != "amd64" || b.Plugin != nil {
		t.Errorf("arch %q plugin %q", b.Arch, b.Plugin)
	}
	if !strings.Contains(string(b.Flow), "100.64.0.1.yolo") {
		t.Errorf("flow %s", b.Flow)
	}
	if got := manifestEngines(t, b.Manifest); !reflect.DeepEqual(got, []string{"100.64.0.1.yolo"}) {
		t.Errorf("manifest engines %v", got)
	}
	eq(t, engineIdent(t, b.Ast), "DetectionEngine_100_64_0_1_yolo")
}

func TestCompileFlowUnknownVehicleResolvesOnOrchestrator(t *testing.T) {
	f := engineFake(t)
	tpl := localYolo()
	tpl.Vehicle = ""
	tpl.Producers = append(tpl.Producers, flows.TemplateProducer{Name: testGCSIP + ".front"})
	tpl.Edges = append(tpl.Edges, flows.TemplateEdge{From: testGCSIP + ".front", To: testGCSIP + ".yolo", Timeout: "1s"})
	s := newFlowService(t, f, flowCatalog("100.64.0.1"))
	rec := do(s.ServeCompile, "POST", "/api/compile", trackBody(t, "100.64.0.1.yolo", tpl, nil))
	if rec.Code != 200 || len(f.builds) != 1 {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	if !strings.Contains(string(f.builds[0].Flow), testGCSIP+".yolo") {
		t.Errorf("flow %s", f.builds[0].Flow)
	}
}

func TestCompileFlowDesignedForNamedHost(t *testing.T) {
	f := engineFake(t)
	s := newFlowService(t, f, flowCatalog("100.64.0.1", "100.64.0.2"))
	rec := do(s.ServeCompile, "POST", "/api/compile", trackBody(t, "swap03.yolo", charlieYolo("swap03"), nil))
	if rec.Code != 200 || !reflect.DeepEqual(decodeMap(t, rec), map[string]any{"ok": true}) {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	eq(t, engineIdent(t, f.builds[0].Ast), "DetectionEngine_100_64_0_2_yolo")
}

func TestCompileFlowCheckFailure(t *testing.T) {
	f := engineFake(t)
	s := newFlowService(t, f, flowCatalog())
	rec := do(s.ServeCompile, "POST", "/api/compile", trackBody(t, "100.64.0.1.det", remoteDet(), nil))
	want := `{"errors":[{"node_id":null,"event_id":null,"message":"flow engine 100.64.0.1.det is not on 100.64.0.1"}]}`
	if rec.Code != 200 || strings.TrimSpace(rec.Body.String()) != want {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	if len(f.builds) != 0 {
		t.Error("compiler was called")
	}
}

func TestCompileFlowWithoutDaemonIs503(t *testing.T) {
	f := engineFake(t)
	s := newService(t, f)
	rec := do(s.ServeCompile, "POST", "/api/compile", trackBody(t, "100.64.0.1.yolo", localYolo(), nil))
	if rec.Code != 503 || decodeMap(t, rec)["detail"] != "the GCS has no connection to the daemon" {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
}

func TestCompileEngineReferenceWithoutFlow(t *testing.T) {
	f := engineFake(t)
	s := newService(t, f)
	rec := do(s.ServeCompile, "POST", "/api/compile", trackBody(t, "yolo", nil, nil))
	want := `{"errors":[{"node_id":null,"event_id":null,"message":"mission uses engine yolo in trk.Engine, but the flow has no such engine"}]}`
	if rec.Code != 200 || strings.TrimSpace(rec.Body.String()) != want {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	if len(f.builds) != 0 {
		t.Error("compiler was called")
	}
}

func TestBuildAndRenderKeepEngineNamesVerbatim(t *testing.T) {
	f := engineFake(t)
	s := newService(t, f)
	body := trackBody(t, "yolo", nil, func(m map[string]any) { m["arch"] = "arm64" })
	if rec := do(s.ServeBuild, "POST", "/api/build", body); rec.Code != 200 {
		t.Fatalf("build status %d: %s", rec.Code, rec.Body)
	}
	eq(t, engineIdent(t, f.builds[0].Ast), "DetectionEngine_yolo")
	if rec := do(s.ServeRenderDSL, "POST", "/api/render_dsl", body); rec.Code != 200 {
		t.Fatalf("render status %d: %s", rec.Code, rec.Body)
	}
}
