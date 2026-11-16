package mission

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"
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
