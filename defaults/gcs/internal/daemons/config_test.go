package daemons

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/rs/zerolog"
)

func glideConfig(t *testing.T) []byte {
	t.Helper()
	raw, err := os.ReadFile("testdata/glide.toml")
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func hashOf(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

// serve runs the route pattern with body and decodes the JSON answer.
func serve(t *testing.T, h *Handler, pattern, target, body string) (int, map[string]any) {
	t.Helper()
	method := strings.SplitN(pattern, " ", 2)[0]
	rec := httptest.NewRecorder()
	h.Routes()[pattern].ServeHTTP(rec, httptest.NewRequest(method, target, strings.NewReader(body)))
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("%s: %d %q: %v", pattern, rec.Code, rec.Body, err)
	}
	return rec.Code, out
}

func TestGetConfig(t *testing.T) {
	raw := glideConfig(t)
	f := &fakeClient{config: raw}
	h := NewHandler(f, self, zerolog.Nop())

	code, out := serve(t, h, "GET /api/daemons/config", "/api/daemons/config?ip=100.64.0.1", "")
	if code != http.StatusOK || out["raw_toml"] != string(raw) || out["config_hash"] != hashOf(raw) {
		t.Fatalf("%d %v", code, out)
	}
	if out["form"] == nil || out["own"] != true || out["self_service"] != "gcs" {
		t.Errorf("own daemon: form %v, own %v, self_service %v", out["form"] != nil, out["own"], out["self_service"])
	}

	code, out = serve(t, h, "GET /api/daemons/config", "/api/daemons/config?ip=100.64.0.2", "")
	if code != http.StatusOK || out["own"] != false || out["self_service"] != "" {
		t.Errorf("remote: %d own %v self_service %v", code, out["own"], out["self_service"])
	}
	if !reflect.DeepEqual(f.keys, []string{"100.64.0.1", "100.64.0.2"}) {
		t.Errorf("route keys = %q", f.keys)
	}

	f.config = []byte("daemon = 3\n")
	code, out = serve(t, h, "GET /api/daemons/config", "/api/daemons/config?ip=100.64.0.2", "")
	if code != http.StatusOK || out["form"] != nil || !strings.Contains(out["form_error"].(string), "daemon") || out["raw_toml"] != "daemon = 3\n" {
		t.Errorf("unrepresentable: %d %v", code, out)
	}

	code, _ = serve(t, h, "GET /api/daemons/config", "/api/daemons/config", "")
	if code != http.StatusBadRequest {
		t.Errorf("no ip: %d", code)
	}
}

func TestConvert(t *testing.T) {
	h := NewHandler(&fakeClient{}, self, zerolog.Nop())
	code, out := serve(t, h, "POST /api/daemons/config/convert", "/api/daemons/config/convert",
		`{"to":"raw","form":{"daemon":{"hostname":"h","port":9000}}}`)
	if code != http.StatusOK || !strings.Contains(out["raw_toml"].(string), "port = 9000") {
		t.Errorf("to raw: %d %v", code, out)
	}

	code, out = serve(t, h, "POST /api/daemons/config/convert", "/api/daemons/config/convert",
		`{"to":"form","raw_toml":"[daemon]\nhostname = \"h\"\n"}`)
	if code != http.StatusOK || out["form"].(map[string]any)["daemon"].(map[string]any)["hostname"] != "h" {
		t.Errorf("to form: %d %v", code, out)
	}

	code, out = serve(t, h, "POST /api/daemons/config/convert", "/api/daemons/config/convert", `{"to":"form","raw_toml":"[daemon"}`)
	detail, _ := out["detail"].(map[string]any)
	if code != http.StatusUnprocessableEntity || detail["line"] != 1.0 {
		t.Errorf("syntax error: %d %v", code, out)
	}

	code, out = serve(t, h, "POST /api/daemons/config/convert", "/api/daemons/config/convert", `{"to":"form","raw_toml":"daemon = 3"}`)
	if code != http.StatusUnprocessableEntity || !strings.Contains(out["detail"].(string), "daemon") {
		t.Errorf("unrepresentable: %d %v", code, out)
	}
}

const missingPkgService = `{"ip":"%s","raw_toml":"[[services]]\nname = \"yolo\"\ntype = \"engine\"\n[services.plugin]\nplugin = \"yolo\"\npackage = \"github.com/org/yolo\"\n"}`

func TestCheck(t *testing.T) {
	f := &fakeClient{packages: packagesResponse(t)}
	h := NewHandler(f, self, zerolog.Nop())

	code, out := serve(t, h, "POST /api/daemons/config/check", "/api/daemons/config/check",
		`{"ip":"100.64.0.2","form":{"services":[{"name":"e","type":"engine","plugin":{"package":"q"}}]}}`)
	if code != http.StatusOK || len(out["errors"].([]any)) != 1 || !strings.Contains(out["errors"].([]any)[0].(string), "no plugin defined") {
		t.Errorf("invalid: %d %v", code, out)
	}

	code, out = serve(t, h, "POST /api/daemons/config/check", "/api/daemons/config/check", strings.Replace(missingPkgService, "%s", "100.64.0.2", 1))
	if code != http.StatusOK || len(out["errors"].([]any)) != 0 || len(out["warnings"].([]any)) != 1 || out["blocking"] != false {
		t.Errorf("remote missing package: %d %v", code, out)
	}

	code, out = serve(t, h, "POST /api/daemons/config/check", "/api/daemons/config/check", strings.Replace(missingPkgService, "%s", "100.64.0.1", 1))
	if code != http.StatusOK || out["blocking"] != true {
		t.Errorf("own missing package: %d %v", code, out)
	}
}

func TestApply(t *testing.T) {
	raw := glideConfig(t)
	apply := func(f *fakeClient, ip, hash string) (int, map[string]any) {
		h := NewHandler(f, self, zerolog.Nop())
		body := strings.Replace(missingPkgService, "%s", ip, 1)
		body = strings.Replace(body, `{"ip"`, `{"config_hash":"`+hash+`","ip"`, 1)
		return serve(t, h, "PUT /api/daemons/config", "/api/daemons/config", body)
	}

	f := &fakeClient{config: raw, packages: packagesResponse(t)}
	code, out := apply(f, "100.64.0.2", "stale")
	if code != http.StatusConflict || out["conflict"] != true || len(f.configured) != 0 {
		t.Errorf("conflict: %d %v, configured %d", code, out, len(f.configured))
	}

	f = &fakeClient{config: raw, packages: packagesResponse(t)}
	code, out = apply(f, "100.64.0.2", hashOf(raw))
	want := "[[services]]\nname = \"yolo\"\ntype = \"engine\"\n[services.plugin]\nplugin = \"yolo\"\npackage = \"github.com/org/yolo\"\n"
	if code != http.StatusOK || out["ok"] != true || !reflect.DeepEqual(f.configured, []string{want}) {
		t.Errorf("remote with a warning: %d %v, configured %q", code, out, f.configured)
	}
	for _, k := range f.keys {
		if k != "100.64.0.2" {
			t.Errorf("route keys = %q, want all 100.64.0.2", f.keys)
			break
		}
	}

	f = &fakeClient{config: raw, packages: packagesResponse(t)}
	code, out = apply(f, "100.64.0.1", hashOf(raw))
	if code != http.StatusUnprocessableEntity || !strings.HasPrefix(out["detail"].(string), "refused on the GCS's own daemon") || len(f.configured) != 0 {
		t.Errorf("own with a warning: %d %v, configured %d", code, out, len(f.configured))
	}

	f = &fakeClient{config: raw, packages: packagesResponse(t)}
	h := NewHandler(f, self, zerolog.Nop())
	code, out = serve(t, h, "PUT /api/daemons/config", "/api/daemons/config",
		`{"ip":"100.64.0.2","config_hash":"`+hashOf(raw)+`","form":{"services":[{"name":"e","type":"engine","plugin":{"package":"q"}}]}}`)
	if code != http.StatusUnprocessableEntity || len(out["errors"].([]any)) != 1 || len(f.configured) != 0 {
		t.Errorf("invalid: %d %v", code, out)
	}
}
