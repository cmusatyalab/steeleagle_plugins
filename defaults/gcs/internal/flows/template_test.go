package flows

import (
	"reflect"
	"strings"
	"testing"

	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
)

func intp(i int) *int { return &i }

func testCatalog() Catalog {
	return BuildCatalog(listResponse(), "100.64.0.1", "cloudlet033")
}

func testTemplate() Template {
	return Template{
		Vehicle:   "100.64.0.2.charlie",
		Producers: []TemplateProducer{{Name: "swap03.front", Tokens: intp(2)}},
		Engines:   []TemplateEngine{{Name: "swap03.yolo"}, {Name: "cloudlet033.h264_encoder"}},
		Edges: []TemplateEdge{
			{From: "swap03.front", To: "swap03.yolo", Timeout: "2s"},
			{From: "swap03.yolo", To: "cloudlet033.h264_encoder", Timeout: "1s"},
		},
	}
}

func charlie() fleet.Vehicle {
	return fleet.Vehicle{Name: "100.64.0.2.charlie", DaemonIP: "100.64.0.2"}
}

func wantErr(t *testing.T, err error, want string) {
	t.Helper()
	if err == nil || err.Error() != want {
		t.Errorf("err = %v, want %q", err, want)
	}
}

func TestParseTemplateTOMLFullNames(t *testing.T) {
	doc := `[[producers]]
name = "swap03.front"
tokens = 2

[[engines]]
name = "cloudlet033.gcs"
location = "local"

[[edges]]
from = "swap03.front"
to = "cloudlet033.gcs"
timeout = "2s"
`
	got, err := ParseTemplateTOML([]byte(doc))
	if err != nil {
		t.Fatal(err)
	}
	want := Template{
		Producers: []TemplateProducer{{Name: "swap03.front", Tokens: intp(2)}},
		Engines:   []TemplateEngine{{Name: "cloudlet033.gcs"}},
		Edges:     []TemplateEdge{{From: "swap03.front", To: "cloudlet033.gcs", Timeout: "2s"}},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %+v, want %+v", got, want)
	}
	if _, err := ParseTemplateTOML([]byte("[[nope]]")); err == nil {
		t.Error("unknown keys should error")
	}
}

func TestEmpty(t *testing.T) {
	var nilT *Template
	if !nilT.Empty() || !(&Template{}).Empty() {
		t.Error("nil and zero templates should be empty")
	}
	tpl := testTemplate()
	if tpl.Empty() {
		t.Error("populated template is empty")
	}
}

func TestResolveLocations(t *testing.T) {
	res, err := testTemplate().Resolve(testCatalog(), "100.64.0.1", charlie(), []string{"front"}, "m", nil)
	if err != nil {
		t.Fatal(err)
	}
	f, err := config.ParseFlowConfigFromBytes(res.Flow)
	if err != nil {
		t.Fatal(err)
	}
	wantP := []config.Producer{{Name: "front", Tokens: intp(2)}}
	if !reflect.DeepEqual(f.Producers, wantP) {
		t.Errorf("producers = %+v, want %+v", f.Producers, wantP)
	}
	wantE := []config.Engine{
		{Name: "100.64.0.2.yolo", Location: "local"},
		{Name: "100.64.0.1.h264_encoder", Location: "remote"},
	}
	if !reflect.DeepEqual(f.Engines, wantE) {
		t.Errorf("engines = %+v, want %+v", f.Engines, wantE)
	}
	wantEdges := []config.Edge{
		{From: "front", To: "100.64.0.2.yolo", Timeout: "2s"},
		{From: "100.64.0.2.yolo", To: "100.64.0.1.h264_encoder", Timeout: "1s"},
	}
	if !reflect.DeepEqual(f.Edges, wantEdges) {
		t.Errorf("edges = %+v, want %+v", f.Edges, wantEdges)
	}
	wantMap := map[string]string{"swap03.yolo": "100.64.0.2.yolo", "cloudlet033.h264_encoder": "100.64.0.1.h264_encoder"}
	if !reflect.DeepEqual(res.Engines, wantMap) {
		t.Errorf("Engines = %v, want %v", res.Engines, wantMap)
	}
}

func TestResolveOrchestratorTarget(t *testing.T) {
	alpha := fleet.Vehicle{Name: "100.64.0.1.alpha", DaemonIP: "100.64.0.1"}
	tpl := Template{
		Vehicle:   "100.64.0.1.alpha",
		Producers: []TemplateProducer{{Name: "cloudlet033.telemetry"}},
		Engines:   []TemplateEngine{{Name: "cloudlet033.h264_encoder"}},
		Edges:     []TemplateEdge{{From: "cloudlet033.telemetry", To: "cloudlet033.h264_encoder", Timeout: "1s"}},
	}
	locOf := func(t *testing.T, tpl Template, target fleet.Vehicle) string {
		t.Helper()
		res, err := tpl.Resolve(testCatalog(), "100.64.0.1", target, []string{}, "m", nil)
		if err != nil {
			t.Fatal(err)
		}
		f, err := config.ParseFlowConfigFromBytes(res.Flow)
		if err != nil {
			t.Fatal(err)
		}
		return f.Engines[0].Location
	}
	if got := locOf(t, tpl, alpha); got != "local" {
		t.Errorf("alpha: encoder is %s, want local", got)
	}
	// A producer's host is only where the flow was designed, so charlie
	// uses its own telemetry.
	res, err := tpl.Resolve(testCatalog(), "100.64.0.1", charlie(), []string{}, "m", nil)
	if err != nil {
		t.Fatal(err)
	}
	f, err := config.ParseFlowConfigFromBytes(res.Flow)
	if err != nil {
		t.Fatal(err)
	}
	if len(f.Producers) != 1 || f.Producers[0].Name != "telemetry" {
		t.Errorf("producers = %+v, want bare telemetry", f.Producers)
	}
	if f.Engines[0].Name != "100.64.0.1.h264_encoder" {
		t.Errorf("engine = %s", f.Engines[0].Name)
	}
	if got := locOf(t, tpl, charlie()); got != "remote" {
		t.Errorf("charlie: encoder is %s, want remote", got)
	}
}

func TestResolveRejectsTarget(t *testing.T) {
	cat := testCatalog()
	cat.Daemons = append(cat.Daemons, Daemon{IP: "100.64.0.3", Hostname: "heisenberg", Engines: []Engine{{Name: "100.64.0.3.yolo", Bare: "yolo"}}})
	third := testTemplate()
	third.Engines[1].Name = "heisenberg.yolo"
	third.Edges[1].To = "heisenberg.yolo"
	cam := testTemplate()
	cam.Producers[0].Name = "swap03.down"
	cam.Edges[0].From = "swap03.down"
	both := third
	both.Producers = []TemplateProducer{{Name: "cloudlet033.front"}}
	both.Edges = append([]TemplateEdge{{From: "cloudlet033.front", To: "swap03.yolo", Timeout: "1s"}}, both.Edges[1:]...)
	tests := []struct {
		name    string
		tpl     Template
		cameras []string
		want    string
	}{
		{"third daemon", third, []string{"front"}, "flow engine heisenberg.yolo is neither on this vehicle's daemon nor on the orchestrator"},
		{"missing camera", cam, []string{"front"}, "flow needs camera down, which this vehicle lacks"},
		{"nil cameras skip the check", cam, nil, ""},
		{"producer host doesn't matter, engine still checked", both, []string{"front"}, "flow engine heisenberg.yolo is neither on this vehicle's daemon nor on the orchestrator"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, err := tc.tpl.Resolve(cat, "100.64.0.1", charlie(), tc.cameras, "m", nil)
			if tc.want == "" {
				if err != nil {
					t.Fatal(err)
				}
				return
			}
			wantErr(t, err, tc.want)
		})
	}
	// Telemetry never needs a camera.
	tel := Template{
		Producers: []TemplateProducer{{Name: "swap03.telemetry"}},
		Engines:   []TemplateEngine{{Name: "swap03.yolo"}},
		Edges:     []TemplateEdge{{From: "swap03.telemetry", To: "swap03.yolo", Timeout: "2s"}},
	}
	if _, err := tel.Resolve(cat, "100.64.0.1", charlie(), []string{}, "m", nil); err != nil {
		t.Errorf("telemetry: %v", err)
	}
}

func TestManifestFromCatalog(t *testing.T) {
	res, err := testTemplate().Resolve(testCatalog(), "100.64.0.1", charlie(), []string{"front"}, "mymission", []string{"scout"})
	if err != nil {
		t.Fatal(err)
	}
	m, err := config.ParseMissionManifestFromBytes(res.Manifest)
	if err != nil {
		t.Fatal(err)
	}
	if m.Info.Name != "mymission" || m.Info.Version != "" || len(m.Info.Squawks) != 0 || !reflect.DeepEqual(m.Info.Roles, []string{"scout"}) {
		t.Errorf("info = %+v", m.Info)
	}
	want := []config.MissionEngineField{
		{Name: "100.64.0.2.yolo", Result: "steeleagle_protocol/v1/messages/result/DetectionResult"},
		{Name: "100.64.0.1.h264_encoder", Result: "messages/result/GenericResult"},
	}
	if !reflect.DeepEqual(m.Engines, want) {
		t.Errorf("engines = %+v, want %+v", m.Engines, want)
	}
}

func TestManifestGenericStandIn(t *testing.T) {
	for _, k := range []string{"Detection", "Classification", "Guidance", "Position", "Generic"} {
		r := "steeleagle_protocol/v1/messages/result/" + k + "Result"
		if got := ManifestResult(r); got != r {
			t.Errorf("ManifestResult(%q) = %q", r, got)
		}
	}
	for _, r := range []string{"steeleagle_protocol/v1/messages/frame/EncodedFrame", ""} {
		if got := ManifestResult(r); got != "messages/result/GenericResult" {
			t.Errorf("ManifestResult(%q) = %q", r, got)
		}
	}
}

func TestCheckOK(t *testing.T) {
	if err := testTemplate().Check(testCatalog(), "100.64.0.1"); err != nil {
		t.Error(err)
	}
	tpl := testTemplate()
	tpl.Producers[0].Name = "100.64.0.2.front"
	tpl.Engines = []TemplateEngine{{Name: "100.64.0.2.yolo"}, {Name: "100.64.0.1.h264_encoder"}}
	tpl.Edges = []TemplateEdge{
		{From: "100.64.0.2.front", To: "100.64.0.2.yolo", Timeout: "2s"},
		{From: "100.64.0.2.yolo", To: "100.64.0.1.h264_encoder", Timeout: "1s"},
	}
	if err := tpl.Check(testCatalog(), "100.64.0.1"); err != nil {
		t.Errorf("IP hosts: %v", err)
	}
}

func TestCheckNameErrors(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*Template)
		want   string
	}{
		{"no host", func(t *Template) { t.Engines[0].Name = "yolo" }, "flow name yolo has no host"},
		{"unknown host", func(t *Template) { t.Engines[0].Name = "heisenberg.yolo" }, "flow host heisenberg is not a known daemon"},
		{"no such engine", func(t *Template) { t.Engines[0].Name = "swap03.nope" }, "flow engine swap03.nope is not on swap03"},
		{"same engine", func(t *Template) {
			t.Engines = []TemplateEngine{{Name: "swap03.yolo"}, {Name: "100.64.0.2.yolo"}}
		}, "flow engines swap03.yolo and 100.64.0.2.yolo are the same engine"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			tpl := testTemplate()
			tc.mutate(&tpl)
			wantErr(t, tpl.Check(testCatalog(), "100.64.0.1"), tc.want)
		})
	}
	// A producer's prefix is the vehicle it was designed on, only a label
	tpl := testTemplate()
	tpl.Producers[0].Name = "alpha.front"
	tpl.Edges[0].From = "alpha.front"
	if err := tpl.Check(testCatalog(), "100.64.0.1"); err != nil {
		t.Errorf("vehicle-named producer: %v", err)
	}
	tpl = testTemplate()
	tpl.Edges[0].Timeout = "fast"
	err := tpl.Check(testCatalog(), "100.64.0.1")
	if err == nil || !strings.Contains(err.Error(), `invalid timeout "fast"`) {
		t.Errorf("err = %v", err)
	}
}

func TestEngineNames(t *testing.T) {
	got, err := testTemplate().EngineNames(testCatalog())
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]string{"swap03.yolo": "100.64.0.2.yolo", "cloudlet033.h264_encoder": "100.64.0.1.h264_encoder"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
	tpl := testTemplate()
	tpl.Engines[0].Name = "yolo"
	_, err = tpl.EngineNames(testCatalog())
	wantErr(t, err, "flow name yolo has no host")
}
