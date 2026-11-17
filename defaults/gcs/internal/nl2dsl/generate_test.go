package nl2dsl

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
)

type checkCompiler struct{ calls int }

func (c *checkCompiler) DraftReference(context.Context, mission.PlanningContext) (map[string]any, error) {
	return map[string]any{"schema": "Wait"}, nil
}

func (c *checkCompiler) CheckDraft(_ context.Context, dsl string, _ mission.PlanningContext, _ func(mission.Graph) []string) mission.Draft {
	c.calls++
	if dsl == "old syntax" {
		return mission.Draft{Status: "parse_failed", Errors: []string{"expected V4 action name"}}
	}
	return mission.Draft{Status: "compiled", DSL: "canonical V4", Graph: &mission.Graph{}}
}

func TestGenerateRepairsRejectedProviderDraft(t *testing.T) {
	attempt := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		attempt++
		body, _ := io.ReadAll(r.Body)
		if r.URL.Path != "/responses" || r.Header.Get("Authorization") != "Bearer fake-key" || !strings.Contains(string(body), draftTool) {
			t.Errorf("Unexpected request path, authentication or missing draft tool")
		}
		dsl := "old syntax"
		if attempt > 1 {
			dsl = "valid syntax"
			if !strings.Contains(string(body), "expected V4 action name") || !strings.Contains(string(body), "old syntax") {
				t.Error("Repair request lost the previous candidate or the compiler diagnostic")
			}
		}
		args, _ := json.Marshal(map[string]string{"dsl": dsl, "summary": "A wait mission"})
		_ = json.NewEncoder(w).Encode(map[string]any{
			"status": "completed", "output": []any{map[string]any{"type": "function_call", "name": draftTool, "arguments": string(args)}},
		})
	}))
	defer server.Close()
	compiler := &checkCompiler{}
	generator := Generator{Model: Provider{Name: "openai", ModelID: "test-model", Key: "fake-key", BaseURL: server.URL}, Compiler: compiler}
	result, err := generator.Generate(t.Context(), Options{Messages: []Message{{Role: "user", Content: "Wait 5 seconds."}}})
	if err != nil || result.Draft.Status != "compiled" || result.Draft.DSL != "canonical V4" || compiler.calls != 2 || len(result.Attempts) != 2 {
		t.Fatalf("Generate = %+v, %v; compiler calls = %d", result, err, compiler.calls)
	}
}

func TestPromptConditionalExampleParses(t *testing.T) {
	_, example, ok := strings.Cut(instructions, "Example V4 DSL with both a completion and a conditional transition:\n")
	if !ok {
		t.Fatal("missing conditional DSL example")
	}
	if _, err := mission.ParseDSL(example + "\n"); err != nil {
		t.Fatalf("prompt example is not valid V4 DSL: %v", err)
	}
}

func TestResolveActionsUsesQualifiedRegistryTypes(t *testing.T) {
	ref := map[string]any{"schema": map[string]any{"actions": map[string]any{
		"different.ClimbToAltitude": map[string]any{"type": "different.ClimbToAltitude"},
		"Wait":                      map[string]any{"type": "actions.Wait"},
	}}}
	steps, missing := resolveActions(ref, [][]string{{"gcsactions.ClimbToAltitude"}, {"actions.Wait"}})
	if !reflect.DeepEqual(missing, []string{"gcsactions.ClimbToAltitude"}) || !reflect.DeepEqual(steps[1], []string{"Wait"}) {
		t.Fatalf("resolved %v, missing %v", steps, missing)
	}
}
