package flows

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func serveDefault(t *testing.T, d DefaultFlow) (int, map[string]any) {
	t.Helper()
	w := httptest.NewRecorder()
	d.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/flows/default", nil))
	var body map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	return w.Code, body
}

const relativeFlow = `[[producers]]
name = "vehicle.rgb"
tokens = 1

[[engines]]
name = "remote.gcs"
location = "remote"

[[edges]]
from = "vehicle.rgb"
to = "remote.gcs"
timeout = "1s"
`

func TestDefaultFlow(t *testing.T) {
	if code, body := serveDefault(t, DefaultFlow{}); code != http.StatusOK || body["configured"] != false {
		t.Errorf("without a default flow: %d %v", code, body)
	}

	path := filepath.Join(t.TempDir(), "planning_flow.toml")
	if err := os.WriteFile(path, []byte(relativeFlow), 0o644); err != nil {
		t.Fatal(err)
	}
	code, body := serveDefault(t, DefaultFlow{Path: path, Vehicle: "steppeeagle"})
	if code != http.StatusOK || body["configured"] != true || body["name"] != "planning_flow" ||
		body["vehicle"] != "steppeeagle" || body["toml"] != relativeFlow || body["version"] == "" {
		t.Fatalf("default flow: %d %v", code, body)
	}
	version := body["version"]

	// The same file is the same version in the same run of the GCS
	if _, body := serveDefault(t, DefaultFlow{Path: path, Vehicle: "steppeeagle"}); body["version"] != version {
		t.Error("the version changed without the file")
	}
	// ...and a new one after a restart, so each run loads it again
	if _, body := serveDefault(t, DefaultFlow{Path: path, Started: "later"}); body["version"] == version {
		t.Error("the version didn't change with the run")
	}
	// An edited file is a new version
	if err := os.WriteFile(path, []byte(relativeFlow+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, body := serveDefault(t, DefaultFlow{Path: path}); body["version"] == version {
		t.Error("the version didn't change with the file")
	}

	if err := os.WriteFile(path, []byte("[nonsense]\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if code, body := serveDefault(t, DefaultFlow{Path: path}); code != http.StatusUnprocessableEntity || body["detail"] == nil {
		t.Errorf("a bad file: %d %v", code, body)
	}
	if code, _ := serveDefault(t, DefaultFlow{Path: filepath.Join(t.TempDir(), "missing.toml")}); code != http.StatusUnprocessableEntity {
		t.Errorf("a missing file: %d", code)
	}
}
