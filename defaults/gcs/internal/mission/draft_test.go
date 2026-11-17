package mission

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"strings"
	"testing"

	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/flows"
	"github.com/rs/zerolog"
)

const waitDraft = `Actions:
    actions.Wait waiting(Duration=5.0)
Mission:
    Start waiting
`

func TestCheckDraftUsesPlanCompiler(t *testing.T) {
	c := newFake(t)
	s := newService(t, c)
	result := s.CheckDraft(t.Context(), waitDraft, PlanningContext{}, nil)
	if result.Status != "compiled" || result.Graph == nil || !strings.Contains(result.DSL, "Duration=5") {
		t.Fatalf("CheckDraft = %+v", result)
	}
	if len(c.builds) != 1 || c.builds[0].Arch != "amd64" {
		t.Fatalf("builds = %+v", c.builds)
	}

	bad := s.CheckDraft(t.Context(), waitDraft, PlanningContext{}, func(Graph) []string { return []string{"wrong duration"} })
	if bad.Status != "task_mismatch" || len(c.builds) != 1 {
		t.Fatalf("invalid draft reached compiler: %+v", bad)
	}
}

func TestCheckDraftNeedsMapContext(t *testing.T) {
	c := newFake(t)
	s := newService(t, c)
	result := s.CheckDraft(t.Context(), exDSL, PlanningContext{}, nil)
	if result.Status != "needs_context" || result.DSL == "" || len(result.Errors) == 0 || len(c.builds) != 0 {
		t.Fatalf("CheckDraft = %+v, builds = %d", result, len(c.builds))
	}
}

func TestDraftEngineRoundTripsThroughApplyAndCompile(t *testing.T) {
	const engine = "100.64.0.1.yolo"
	const sdkIdent = "DetectionEngine_local_yolo"
	for name, value := range map[string]string{
		"quoted template name": `"100.64.0.1.yolo"`,
		"legacy identifier":    "DetectionEngine_100_64_0_1_yolo",
		"relative identifier":  sdkIdent,
	} {
		t.Run(name, func(t *testing.T) {
			c := engineFake(t)
			s := newFlowService(t, c, flowCatalog("100.64.0.1"))
			plan := PlanningContext{Flow: localYolo()}
			candidate := "Actions:\n    actions.Track trk(Engine=" + value + ")\nMission:\n    Start trk\n"
			draft := s.CheckDraft(t.Context(), candidate, plan, nil)
			if draft.Status != "compiled" || !strings.Contains(draft.DSL, `Engine="`+engine+`"`) {
				t.Fatalf("CheckDraft = %+v", draft)
			}

			parseRequest, err := json.Marshal(map[string]string{"dsl": draft.DSL})
			if err != nil {
				t.Fatal(err)
			}
			parse := do(s.ServeParseDSL, http.MethodPost, "/api/parse_dsl", parseRequest)
			if parse.Code != http.StatusOK {
				t.Fatalf("parse status %d: %s", parse.Code, parse.Body)
			}
			graph := decodeMap(t, parse)
			eq(t, graph["nodes"].([]any)[0].(map[string]any)["params"], map[string]any{"Engine": engine})

			graph["flow"] = plan.Flow
			compileRequest, err := json.Marshal(graph)
			if err != nil {
				t.Fatal(err)
			}
			compile := do(s.ServeCompile, http.MethodPost, "/api/compile", compileRequest)
			if compile.Code != http.StatusOK {
				t.Fatalf("compile status %d: %s", compile.Code, compile.Body)
			}
			eq(t, decodeMap(t, compile), map[string]any{"ok": true})
			if len(c.builds) != 2 {
				t.Fatalf("build count = %d, want draft and canvas builds", len(c.builds))
			}
			for _, build := range c.builds {
				eq(t, engineIdent(t, build.Ast), sdkIdent)
			}
		})
	}
}

func TestDraftWithUnboundEngineNeedsContext(t *testing.T) {
	c := engineFake(t)
	s := newFlowService(t, c, flowCatalog("100.64.0.1"))
	const dsl = "Actions:\n    actions.Track trk(Engine=DetectionEngine_local_ghost)\nMission:\n    Start trk\n"
	result := s.CheckDraft(t.Context(), dsl, PlanningContext{Flow: localYolo()}, nil)
	if result.Status != "needs_context" || result.Graph == nil || len(c.builds) != 0 {
		t.Fatalf("CheckDraft = %+v, builds = %d", result, len(c.builds))
	}
}

func TestTrackBindingUsesTheCameraRoute(t *testing.T) {
	const raw = "steeleagle_protocol/v1/messages/frame/RawFrame"
	const encoded = "steeleagle_protocol/v1/messages/frame/EncodedFrame"
	const detector = testGCSIP + ".yolo"
	const dsl = `Actions:
    actions.Track track_person(Camera=params.UNBOUND_camera, Engine="UNBOUND_DETECTION_ENGINE", Class="person")
Mission:
    Start track_person
`
	for _, tc := range []struct {
		name      string
		accepts   string
		transform bool
		ambiguous bool
		wantBound bool
	}{
		{name: "direct camera frames", accepts: raw, wantBound: true},
		{name: "config 0.0.8 forwarded conversion", accepts: encoded, transform: true, wantBound: true},
		{name: "incompatible camera frames", accepts: encoded},
		{name: "two matching detector routes", accepts: raw, ambiguous: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := engineFake(t)
			c.registry["actions"].(map[string]any)["actions.Track"].(map[string]any)["fields"] = []any{
				map[string]any{"name": "Camera", "type": "params.Camera"},
				map[string]any{"name": "Engine", "type": "params.DetectionEngine"},
				map[string]any{"name": "Class", "type": "string"},
			}
			c.registry["enums"].(map[string]any)["params.Camera"] = map[string]any{
				"fields": []any{map[string]any{"name": "Camera_front"}},
			}
			plugin, err := os.ReadFile("testdata/driver.plugin.toml")
			if err != nil {
				t.Fatal(err)
			}
			cat := flowCatalog(testGCSIP)
			daemon := &cat.Daemons[0]
			vehicle := &daemon.Vehicles[0]
			vehicle.Streams = append(vehicle.Streams, flows.Stream{Name: "front", Kind: "camera", Type: raw})
			daemon.Engines[0].Accepts = []string{tc.accepts}
			const producer = "alpha.front"
			flow := &flows.Template{
				Vehicle: vehicle.Name, Producers: []flows.TemplateProducer{{Name: producer}},
				Engines: []flows.TemplateEngine{{Name: detector}},
				Edges:   []flows.TemplateEdge{{From: producer, To: detector, Timeout: "1s"}},
			}
			if tc.transform {
				const encoder = testGCSIP + ".encoder"
				daemon.Engines = append(daemon.Engines, flows.Engine{Name: encoder, Bare: "encoder", Accepts: []string{raw}, Forwards: encoded, Typed: true})
				flow.Engines = append(flow.Engines, flows.TemplateEngine{Name: encoder})
				flow.Edges[0].To = encoder
				flow.Edges = append(flow.Edges, flows.TemplateEdge{From: encoder, To: detector, Timeout: "1s"})
			}
			if tc.ambiguous {
				second := daemon.Engines[0]
				second.Name, second.Bare = testGCSIP+".other", "other"
				daemon.Engines = append(daemon.Engines, second)
				flow.Engines = append(flow.Engines, flows.TemplateEngine{Name: second.Name})
				flow.Edges = append(flow.Edges, flows.TemplateEdge{From: producer, To: second.Name, Timeout: "1s"})
			}
			plan := PlanningContext{Flow: flow, Vehicle: vehicle.Name}
			s := NewService(c, testConfig(t), testGCSIP,
				func(context.Context) (flows.Catalog, error) { return cat, nil },
				func(context.Context) ([]fleet.Vehicle, error) {
					return []fleet.Vehicle{{Name: vehicle.Name, Config: plugin, Driver: &config.DriverCap{}}}, nil
				}, zerolog.Nop())
			ref, err := s.DraftReference(t.Context(), plan)
			if err != nil {
				t.Fatal(err)
			}
			context := ref["planning_context"].(map[string]any)
			eq(t, context["cameras"], []cameraBinding{{Name: "front", Constant: "params.Camera_front", FrameType: raw}})
			if tc.wantBound {
				eq(t, context["engines"], []engineBinding{{
					Name: detector, FullName: detector, Constant: "params.DetectionEngine_local_yolo", EngineType: "DetectionEngine",
					ResultType: daemon.Engines[0].Result, CameraNames: []string{"front"},
				}})
			}
			draft := s.CheckDraft(t.Context(), dsl, plan, nil)
			if tc.wantBound {
				if draft.Status != "compiled" || !strings.Contains(draft.DSL, `Camera=params.Camera_front`) ||
					!strings.Contains(draft.DSL, `Engine="`+detector+`"`) || len(c.builds) != 1 {
					t.Fatalf("draft = %+v, builds = %d", draft, len(c.builds))
				}
				for _, attr := range asList(asMap(astDecls(c.builds[0].Ast, "Actions")[0])["Attrs"]) {
					if asMap(attr)["Key"] == "Engine" {
						eq(t, asMap(asMap(attr)["Value"])["Ident"], "DetectionEngine_local_yolo")
					}
				}
			} else if draft.Status != "needs_context" || draft.Graph == nil || len(draft.Errors) != 2 || len(c.builds) != 0 {
				t.Fatalf("draft = %+v, builds = %d", draft, len(c.builds))
			}
		})
	}
}

func TestTrackPreviewRetainsUnboundEngineUntilCompile(t *testing.T) {
	c := engineFake(t)
	s := newFlowService(t, c, flowCatalog(testGCSIP))
	body := trackBody(t, "UNBOUND_detection_engine", localYolo(), nil)
	preview := do(s.ServeRenderDSL, http.MethodPost, "/api/render_dsl", body)
	if preview.Code != http.StatusOK || !strings.Contains(decodeMap(t, preview)["dsl"].(string), `Engine="UNBOUND_detection_engine"`) {
		t.Fatalf("render status %d: %s", preview.Code, preview.Body)
	}
	compiled := do(s.ServeCompile, http.MethodPost, "/api/compile", body)
	if compiled.Code != http.StatusOK || !strings.Contains(compiled.Body.String(), `"node_id":"trk"`) ||
		!strings.Contains(compiled.Body.String(), "Flow Builder") || len(c.builds) != 0 {
		t.Fatalf("unbound graph reached build: status %d: %s", compiled.Code, compiled.Body)
	}
}

func TestCompileIdentifiesMalformedCameraField(t *testing.T) {
	c := engineFake(t)
	track := c.registry["actions"].(map[string]any)["actions.Track"].(map[string]any)
	track["fields"] = append(track["fields"].([]any), map[string]any{"name": "Camera", "type": "params.Camera"})
	s := newFlowService(t, c, flowCatalog(testGCSIP))
	body := trackBody(t, testGCSIP+".yolo", localYolo(), func(m map[string]any) {
		params := m["nodes"].([]any)[0].(map[string]any)["params"].(map[string]any)
		params["Camera"] = map[string]any{"label": "(no values)", "value": nil}
	})
	rec := do(s.ServeCompile, http.MethodPost, "/api/compile", body)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"node_id":"trk"`) ||
		!strings.Contains(rec.Body.String(), "trk.Camera: Expected a camera identifier.") || len(c.builds) != 0 {
		t.Fatalf("malformed Camera was not identified before build: status %d: %s", rec.Code, rec.Body)
	}
}
