package chat

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/nl2dsl"
)

type conversationCompiler struct{}

func (conversationCompiler) DraftReference(context.Context, mission.PlanningContext) (map[string]any, error) {
	return map[string]any{"actions": []string{"Wait"}}, nil
}

func (conversationCompiler) CheckDraft(context.Context, string, mission.PlanningContext, func(mission.Graph) []string) mission.Draft {
	return mission.Draft{Status: "compiled", DSL: "draft accepted by V4", Graph: &mission.Graph{}}
}

func TestChatStreamsValidatedArtifact(t *testing.T) {
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/messages" || r.Header.Get("x-api-key") != "fake-key" {
			t.Error("Unexpected Anthropic request")
		}
		_, _ = w.Write([]byte(`{"content":[{"type":"tool_use","name":"submit_mission_dsl","input":{"dsl":"candidate","summary":"A wait mission"}}]}`))
	}))
	defer model.Close()
	provider := nl2dsl.Provider{Name: "anthropic", ModelID: "test", Key: "fake-key", BaseURL: model.URL}
	mux := http.NewServeMux()
	New(provider, nl2dsl.Generator{Model: provider, Compiler: conversationCompiler{}}, Credentials{Anthropic: true}).Register(mux)
	request := httptest.NewRequest(http.MethodPost, "/api/chat", strings.NewReader(`{"messages":[{"role":"user","content":"Wait five seconds."}]}`))
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, request)
	body := w.Body.String()
	for _, want := range []string{"event: status", "event: done", "draft accepted by V4", "Apply draft to Mission Builder"} {
		if !strings.Contains(body, want) {
			t.Fatalf("chat SSE missing %q: %s", want, body)
		}
	}
	w = httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/chat/status", nil))
	if !strings.Contains(w.Body.String(), `"configured":true`) || strings.Contains(w.Body.String(), "fake-key") {
		t.Fatalf("bad status: %s", w.Body.String())
	}
}
