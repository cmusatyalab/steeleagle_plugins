package daemons

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"github.com/rs/zerolog"
)

const legacyConfig = `# Original comment
unknown_table = { enabled = true }

[[services]]
name = "operator-ui"
type = "engine"
[services.plugin]
plugin = "gcs"
package = "` + gcsPkg + `"
[services.plugin.args]
mapbox = "keep-ui-value"
"OPENAI_AI_API" = '''demo-legacy-value
legacy-continued'''
ai = { chat_provider = "openai", "openai-api-key" = "demo-openai-value" }

[[services]]
name = "gcs"
type = "engine"
plugin = { plugin = "other", package = "example.org/other", args = { OPENAI_AI_API = "other-plugin-argument" } }
`

func TestGetConfigOmitsLegacyGCSCredentials(t *testing.T) {
	f := &fakeClient{config: []byte(legacyConfig), packages: packagesResponse(t)}
	h := NewHandler(f, self, zerolog.Nop())
	code, out := serve(t, h, "GET /api/daemons/config", "/api/daemons/config?ip=100.64.0.2", "")
	if code != http.StatusOK || out["config_hash"] != hashOf(f.config) || out["config_notice"] == nil {
		t.Fatalf("GET = %d %v", code, out)
	}
	assertNoCredentialValues(t, out)
	raw := out["raw_toml"].(string)
	if strings.Contains(raw, "demo-legacy-value") || strings.Contains(raw, "legacy-continued") ||
		!strings.Contains(raw, "other-plugin-argument") || !strings.Contains(raw, "keep-ui-value") || !strings.Contains(raw, "unknown_table") {
		t.Fatalf("unexpected sanitized TOML: %s", raw)
	}
	services := out["form"].(map[string]any)["services"].([]any)
	args := services[0].(map[string]any)["plugin"].(map[string]any)["args"].(map[string]any)
	if len(args) != 2 || args["ai"].(map[string]any)["chat_provider"] != "openai" {
		t.Fatalf("sanitized UI args = %v", args)
	}
	if len(f.configured) != 0 {
		t.Fatal("GET changed the daemon configuration")
	}
	body, err := json.Marshal(map[string]any{"ip": "100.64.0.2", "config_hash": out["config_hash"], "raw_toml": raw})
	if err != nil {
		t.Fatal(err)
	}
	code, out = serve(t, h, "PUT /api/daemons/config", "/api/daemons/config", string(body))
	if code != http.StatusOK || len(f.configured) != 1 || f.configured[0] != raw {
		t.Fatalf("applying the cleaned TOML = %d %v, daemon updates = %v", code, out, f.configured)
	}
}

func TestConfigRejectsGCSCredentialsInRawAndForm(t *testing.T) {
	for _, tc := range []struct {
		name    string
		format  string
		input   any
		keys    []string
		convert string
	}{
		{"TOML", "raw_toml", legacyConfig, []string{"OPENAI_AI_API", "OPENAI_API_KEY"}, "form"},
		{"form", "form", json.RawMessage(`{"services":[{"name":"any-ui-name","type":"engine","plugin":{"plugin":"gcs","package":"x","args":{"ai":{"nested":[{"AnthropicApiKey":"demo-anthropic-value"}]}}}}]}`), []string{"ANTHROPIC_API_KEY"}, "raw"},
		{"mixed case tables, dotted and escaped keys", "raw_toml", `[[Services]]
name = "operator-ui"
type = "engine"
[Services.Plugin]
Plugin = "gcs"
package = "x"
[Services.Plugin.Args]
ai."\u004fPENAI_API_KEY" = "demo-openai-value"
`, []string{"OPENAI_API_KEY"}, "form"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := &fakeClient{config: []byte("[daemon]\n"), packages: packagesResponse(t)}
			h := NewHandler(f, self, zerolog.Nop())
			body, err := json.Marshal(map[string]any{
				"ip": "100.64.0.2", "config_hash": hashOf(f.config), tc.format: tc.input, "to": tc.convert,
			})
			if err != nil {
				t.Fatal(err)
			}
			for _, route := range []struct {
				method, path string
				status       int
			}{
				{"POST", "/api/daemons/config/check", http.StatusOK},
				{"PUT", "/api/daemons/config", http.StatusUnprocessableEntity},
				{"POST", "/api/daemons/config/convert", http.StatusUnprocessableEntity},
			} {
				code, out := serve(t, h, route.method+" "+route.path, route.path, string(body))
				if code != route.status || (code == http.StatusOK && out["blocking"] != true) || len(f.configured) != 0 {
					t.Fatalf("%s %s = %d %v, daemon updates = %v", route.method, route.path, code, out, f.configured)
				}
				response := assertNoCredentialValues(t, out)
				for _, key := range tc.keys {
					if !strings.Contains(response, key) {
						t.Errorf("missing remediation for %s in %s", key, response)
					}
				}
			}
		})
	}
}

func TestConfigSyntaxDiagnosticOmitsOriginalTokens(t *testing.T) {
	const invalid = `[[services]]
name = "operator-ui"
type = "engine"
plugin.plugin = "gcs"
plugin.package = "x"
plugin.args.OPENAI_API_KEY = demo-openai-value
`
	f := &fakeClient{config: []byte(invalid)}
	h := NewHandler(f, self, zerolog.Nop())
	body, err := json.Marshal(map[string]any{"ip": "100.64.0.2", "config_hash": hashOf(f.config), "to": "form", "raw_toml": invalid})
	if err != nil {
		t.Fatal(err)
	}
	for _, route := range []struct {
		method, path string
		status       int
	}{
		{"POST", "/api/daemons/config/check", http.StatusOK},
		{"POST", "/api/daemons/config/convert", http.StatusUnprocessableEntity},
		{"PUT", "/api/daemons/config", http.StatusUnprocessableEntity},
	} {
		code, out := serve(t, h, route.method+" "+route.path, route.path, string(body))
		response := assertNoCredentialValues(t, out)
		if code != route.status || !strings.Contains(response, "Invalid TOML") || len(f.configured) != 0 {
			t.Errorf("%s %s = %d %s, daemon updates = %d", route.method, route.path, code, response, len(f.configured))
		}
	}
	code, out := serve(t, h, "GET /api/daemons/config", "/api/daemons/config?ip=100.64.0.2", "")
	if code != http.StatusUnprocessableEntity || out["raw_toml"] != nil || out["form"] != nil {
		t.Fatalf("GET invalid TOML = %d %v", code, out)
	}
	assertNoCredentialValues(t, out)
}

func assertNoCredentialValues(t *testing.T, out map[string]any) string {
	t.Helper()
	response, err := json.Marshal(out)
	if err != nil {
		t.Fatal(err)
	}
	for _, value := range []string{"demo-legacy-value", "legacy-continued", "demo-openai-value", "demo-anthropic-value"} {
		if strings.Contains(string(response), value) {
			t.Fatal("response included a value from a legacy credential field")
		}
	}
	return string(response)
}
