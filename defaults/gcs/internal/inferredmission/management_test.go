package inferredmission

import (
	"encoding/json"
	"mime"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func savedRun(t *testing.T, s *Service, status string) *run {
	t.Helper()
	const id = "c869d1af-7557-41d2-a505-f15c693b54bc"
	dir := filepath.Join(s.root, "runs", id)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	created := time.Date(2026, 11, 12, 22, 21, 27, 0, time.UTC)
	r := &run{
		dir: dir, changed: make(chan struct{}), rows: map[int]section{},
		meta: session{
			ID: id, SchemaVersion: 1, Mode: "analysis_only", Source: "uploaded_video",
			Status: status, Settings: s.settings, Models: s.models(),
			Created: seconds(created), Updated: seconds(created.Add(time.Minute)),
			Video: &Video{Name: "航拍 Café.MP4"},
		},
	}
	if err := writeJSONFile(filepath.Join(dir, "session.json"), r.meta); err != nil {
		t.Fatal(err)
	}
	s.runs[id] = r
	return r
}

func managementClient(s *Service) func(method, path, body string) *httptest.ResponseRecorder {
	mux := http.NewServeMux()
	s.Register(mux, func() []Source { return nil })
	return func(method, path, body string) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, httptest.NewRequest(method, api+path, strings.NewReader(body)))
		return w
	}
}

func TestRunManagementPreservesAnalysisClock(t *testing.T) {
	root := t.TempDir()
	service := testService(t, root, nil, nil)
	r := savedRun(t, service, "paused")
	original := r.meta
	request := managementClient(service)
	endpoint := "/sessions/" + original.ID

	for _, invalid := range []string{`{"display_name":"  "}`, `{"display_name":"` + strings.Repeat("航", 121) + `"}`} {
		if w := request(http.MethodPatch, endpoint, invalid); w.Code != http.StatusUnprocessableEntity {
			t.Fatalf("invalid name status = %d: %s", w.Code, w.Body)
		}
	}
	_, changed := r.snap()
	w := request(http.MethodPatch, endpoint, `{"display_name":"  360p Café / Recency  ","archived":true}`)
	var meta session
	if w.Code != http.StatusOK || json.Unmarshal(w.Body.Bytes(), &meta) != nil {
		t.Fatalf("rename/archive status = %d: %s", w.Code, w.Body)
	}
	if meta.Name != "360p Café / Recency" || meta.Archived == nil || meta.Updated != original.Updated || meta.ID != original.ID || meta.Revision <= original.Revision {
		t.Fatalf("unexpected metadata after rename/archive: %+v", meta)
	}
	select {
	case <-changed:
	default:
		t.Fatal("rename/archive did not publish a session update")
	}
	if err := service.resume(r); err == nil || !strings.Contains(err.Error(), "Restore") {
		t.Fatalf("resume archived run: %v", err)
	}

	service.Close()
	reopened := testService(t, root, nil, nil)
	request = managementClient(reopened)
	w = request(http.MethodGet, "/sessions", "")
	if w.Code != http.StatusOK || strings.TrimSpace(w.Body.String()) != "[]" {
		t.Fatalf("default list leaked archived run: %d: %s", w.Code, w.Body)
	}
	w = request(http.MethodGet, "/sessions?include_archived=true", "")
	var list []snapshot
	if w.Code != http.StatusOK || json.Unmarshal(w.Body.Bytes(), &list) != nil || len(list) != 1 {
		t.Fatalf("archived list = %d: %s", w.Code, w.Body)
	}
	if list[0].Archived == nil || list[0].Name != meta.Name || list[0].Updated != original.Updated {
		t.Fatalf("archived metadata did not survive restart: %+v", list[0].session)
	}

	w = request(http.MethodPatch, endpoint, `{"archived":false}`)
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"archived_at":null`) {
		t.Fatalf("restore status = %d: %s", w.Code, w.Body)
	}
	w = request(http.MethodGet, "/sessions", "")
	list = nil
	if w.Code != http.StatusOK || json.Unmarshal(w.Body.Bytes(), &list) != nil || len(list) != 1 {
		t.Fatalf("restored list = %d: %s", w.Code, w.Body)
	}
	if list[0].Archived != nil || list[0].Updated != original.Updated || list[0].Status != "paused" {
		t.Fatalf("restore modified the analysis state: %+v", list[0].session)
	}
}

func TestArchiveRefusesDrainingRunWithoutPartialUpdate(t *testing.T) {
	service := testService(t, t.TempDir(), nil, nil)
	r := savedRun(t, service, "pausing")
	request := managementClient(service)
	w := request(http.MethodPatch, "/sessions/"+r.meta.ID, `{"display_name":"Must not persist","archived":true}`)
	if w.Code != http.StatusConflict {
		t.Fatalf("archive draining status = %d: %s", w.Code, w.Body)
	}
	state, _ := r.snap()
	if state.Archived != nil || state.Name != "" {
		t.Fatalf("failed archive changed metadata: %+v", state.session)
	}
}

func TestJSONDownloadUsesUTF8NamesAndOriginalArtifact(t *testing.T) {
	service := testService(t, t.TempDir(), nil, nil)
	r := savedRun(t, service, "completed")
	row := section{
		sectionSummary: sectionSummary{Index: 1, Status: "completed"},
		DSL:            "Mission:", Mission: &missionArtifact{SchemaVersion: 1},
	}
	if err := r.setSection(row); err != nil {
		t.Fatal(err)
	}
	request := managementClient(service)
	endpoint := "/sessions/" + r.meta.ID
	path := endpoint + "/sections/1/files/"
	w := request(http.MethodGet, path+"mission.json", "")
	assertArtifactName(t, w, "航拍-Café_20261112T222127.000Z_section-000001_mission.json")
	if !strings.Contains(w.Body.String(), `"schema_version":1`) {
		t.Fatal("mission JSON lost its V4 graph artifact")
	}

	w = request(http.MethodPatch, endpoint, `{"display_name":" 360p Café / Recency "}`)
	if w.Code != http.StatusOK {
		t.Fatalf("rename status = %d: %s", w.Code, w.Body)
	}
	w = request(http.MethodGet, path+"inference.json", "")
	assertArtifactName(t, w, "360p-Café-Recency_20261112T222127.000Z_section-000001_inference.json")
	if strings.Contains(w.Body.String(), `"frame_files"`) {
		t.Fatal("inference JSON leaked internal frame paths")
	}

	w = request(http.MethodGet, path+"mission.dsl", "")
	if w.Code != http.StatusOK || w.Header().Get("Content-Disposition") != `attachment; filename="section-000001-mission.dsl"` {
		t.Fatalf("unexpected DSL download: %d %q", w.Code, w.Header().Get("Content-Disposition"))
	}
}

func assertArtifactName(t *testing.T, w *httptest.ResponseRecorder, want string) {
	t.Helper()
	header := w.Header().Get("Content-Disposition")
	disposition, params, err := mime.ParseMediaType(header)
	if w.Code != http.StatusOK || err != nil || disposition != "attachment" || params["filename"] != want {
		t.Fatalf("status = %d, disposition = %q, parsed filename = %q, error = %v, want filename %q", w.Code, header, params["filename"], err, want)
	}
	first, encoded, ok := strings.Cut(header, "; filename*=UTF-8''")
	unescaped, err := url.PathUnescape(encoded)
	if !ok || err != nil || unescaped != want || strings.ContainsFunc(first, func(r rune) bool { return r > 127 }) {
		t.Fatalf("download did not include both a Unicode name and an ASCII fallback: %q", header)
	}
}

func TestLegacyRunClockUsesSavedSessionModificationTime(t *testing.T) {
	root := t.TempDir()
	service := testService(t, root, nil, nil)
	r := savedRun(t, service, "completed")
	legacy := r.meta
	legacy.Updated = 0
	path := filepath.Join(r.dir, "session.json")
	if err := writeJSONFile(path, legacy); err != nil {
		t.Fatal(err)
	}
	updated := time.Date(2026, 11, 12, 22, 22, 28, 0, time.UTC)
	if err := os.Chtimes(path, updated, updated); err != nil {
		t.Fatal(err)
	}
	service.Close()

	reopened := testService(t, root, nil, nil)
	got := reopened.runs[legacy.ID]
	if got == nil {
		t.Fatal("saved legacy run was not recovered")
	}
	state, _ := got.snap()
	if state.Updated != seconds(updated) || state.Status != "completed" {
		t.Fatalf("unexpected legacy analysis clock: %+v", state.session)
	}
}
