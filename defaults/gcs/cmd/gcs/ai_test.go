package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/aiconfig"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/nl2dsl"
)

func TestAIProvidersUseStoredKeyChanges(t *testing.T) {
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("OPENAI_API_KEY", "")
	t.Setenv("ANTHROPIC_API_KEY", "")
	store, err := openAICredentials("127.0.0.1.gcs")
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	store.Register(mux)
	chat, vision := aiProvider(aiconfig.Defaults(), store)
	if chat.Configured() || vision.Configured() {
		t.Fatal("providers started with a key despite an empty store and environment")
	}

	seen := make(chan string, 4)
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen <- r.Header.Get("Authorization")
		_, _ = w.Write([]byte(`{"output":[{"type":"output_text","text":"fixture"}]}`))
	}))
	defer model.Close()
	chat.BaseURL, vision.BaseURL = model.URL, model.URL

	change := func(method, payload string) {
		t.Helper()
		req := httptest.NewRequest(method, "http://localhost:8002/api/ai/credentials/openai", strings.NewReader(payload))
		req.RemoteAddr = "127.0.0.1:1234"
		req.Header.Set("Origin", "http://localhost:8002")
		if payload != "" {
			req.Header.Set("Content-Type", "application/json")
		}
		res := httptest.NewRecorder()
		mux.ServeHTTP(res, req)
		if res.Code != http.StatusOK {
			t.Fatalf("%s credential returned %d: %s", method, res.Code, res.Body)
		}
	}

	for _, key := range []string{"first-fixture-key", "replacement-fixture-key"} {
		change(http.MethodPut, `{"key":"`+key+`"}`)
		for _, provider := range []nl2dsl.Provider{chat, vision} {
			if _, err := provider.Complete(context.Background(), nl2dsl.Request{}); err != nil {
				t.Fatal(err)
			}
			if got := <-seen; got != "Bearer "+key {
				t.Fatal("provider request did not use the current credential")
			}
		}
	}
	sameService, err := openAICredentials("127.0.0.2.gcs")
	if err != nil || sameService.Key("openai") != "replacement-fixture-key" {
		t.Fatalf("credential did not survive reopen under the same service name: %v", err)
	}
	otherService, err := openAICredentials("127.0.0.2.another-gcs")
	if err != nil || otherService.Key("openai") != "" {
		t.Fatalf("a different GCS service inherited the saved key: %v", err)
	}
	change(http.MethodDelete, "")
	if chat.Configured() || vision.Configured() {
		t.Fatal("providers still report a key after it was cleared")
	}
}
