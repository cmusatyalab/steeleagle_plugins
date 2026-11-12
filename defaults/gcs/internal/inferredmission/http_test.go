package inferredmission

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/nl2dsl"
	"github.com/rs/zerolog"
)

type videoCompiler struct{}

func (videoCompiler) DraftReference(context.Context, mission.PlanningContext) (map[string]any, error) {
	return map[string]any{"schema": map[string]any{"actions": map[string]any{"Wait": nil, "ReturnToHome": nil}}}, nil
}

func (videoCompiler) CheckDraft(_ context.Context, dsl string, _ mission.PlanningContext, check func(mission.Graph) []string) mission.Draft {
	start := "wait"
	g := mission.Graph{StartID: &start,
		Nodes: []mission.GraphNode{{InstanceID: start, TypeName: "Wait", Params: map[string]any{"Duration": 5}},
			{InstanceID: "home", TypeName: "ReturnToHome"}},
		Edges: []mission.GraphEdge{{Source: start, EventID: "done", Target: "home"}}}
	if issues := check(g); len(issues) > 0 {
		return mission.Draft{Status: "task_mismatch", Errors: issues}
	}
	return mission.Draft{Status: "compiled", DSL: dsl, Graph: &g, Errors: []string{}}
}

func testService(t *testing.T, root string, block chan struct{}, entered chan struct{}) *Service {
	t.Helper()
	var requests atomic.Int32
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if bytes.Contains(body, []byte("input_image")) {
			if requests.Add(1) == 1 && block != nil {
				close(entered)
				select {
				case <-block:
				case <-r.Context().Done():
					return
				}
			}
			_, _ = io.WriteString(w, `{"output":[{"type":"message","content":[{"type":"output_text","text":"Wait 5 seconds, then return home."}]}]}`)
			return
		}
		_, _ = io.WriteString(w, `{"output":[{"type":"function_call","name":"submit_mission_dsl","arguments":"{\"dsl\":\"V4 Wait draft\",\"summary\":\"Wait then return.\"}"}]}`)
	}))
	t.Cleanup(model.Close)
	provider := nl2dsl.Provider{Name: "openai", ModelID: "test", Key: "fake-key", BaseURL: model.URL}
	cfg := DefaultSettings()
	cfg.SectionSeconds = 2
	service, err := New(t.Context(), root, cfg, provider, provider, nl2dsl.Generator{Model: provider, Compiler: videoCompiler{}}, zerolog.Nop())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(service.Close)
	return service
}

func waitStatus(t *testing.T, r *run, want string) snapshot {
	t.Helper()
	timer := time.NewTimer(12 * time.Second)
	defer timer.Stop()
	for {
		state, changed := r.snap()
		if state.Status == want {
			return state
		}
		select {
		case <-changed:
		case <-timer.C:
			t.Fatalf("status = %s, want %s: %+v", state.Status, want, state)
		}
	}
}

func TestVideoUploadPauseResumeAndDownload(t *testing.T) {
	if !mediaAvailable() {
		t.Skip("FFmpeg and ffprobe are required")
	}
	root := t.TempDir()
	clip := filepath.Join(root, "fixture.mp4")
	cmd := exec.CommandContext(t.Context(), "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=96x64:r=10:d=3",
		"-c:v", "libx264", "-threads", "1", clip)
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("Create test clip: %v: %s", err, output)
	}
	block, entered := make(chan struct{}), make(chan struct{})
	s := testService(t, root, block, entered)
	mux := http.NewServeMux()
	s.Register(mux, func() []Source { return nil })
	var upload bytes.Buffer
	form := multipart.NewWriter(&upload)
	part, err := form.CreateFormFile("file", "fixture.mp4")
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(clip)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = part.Write(data)
	_ = form.Close()
	request := httptest.NewRequest(http.MethodPost, api+"/videos", &upload)
	request.Header.Set("Content-Type", form.FormDataContentType())
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, request)
	var video Video
	if w.Code != http.StatusCreated || json.Unmarshal(w.Body.Bytes(), &video) != nil {
		t.Fatalf("upload = %d: %s", w.Code, w.Body)
	}
	send := func(method, path, data string) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, httptest.NewRequest(method, api+path, strings.NewReader(data)))
		return w
	}
	w = send("POST", "/sessions", `{"source_type":"uploaded_video","mode":"analysis_only","video_asset_id":"`+video.ID+`"}`)
	var state snapshot
	if w.Code != http.StatusCreated || json.Unmarshal(w.Body.Bytes(), &state) != nil {
		t.Fatalf("start = %d: %s", w.Code, w.Body)
	}
	r := s.runs[state.ID]
	select {
	case <-entered:
	case <-time.After(10 * time.Second):
		got, _ := r.snap()
		t.Fatalf("Vision request never arrived: %+v", got.Sections)
	}
	pause := send("POST", "/sessions/"+state.ID+"/pause", "")
	close(block)
	if pause.Code != http.StatusOK {
		t.Fatalf("pause = %d: %s", pause.Code, pause.Body)
	}
	state = waitStatus(t, r, "paused")
	if len(state.Sections) != 1 || state.Sections[0].Status != "completed" {
		t.Fatalf("pause did not preserve the completed section: %+v", state.Sections)
	}
	w = send("POST", "/sessions/"+state.ID+"/resume", "")
	if w.Code != http.StatusOK {
		t.Fatalf("resume = %d: %s", w.Code, w.Body)
	}
	state = waitStatus(t, r, "completed")
	if len(state.Sections) != 2 || state.Sections[1].Start != 2 || state.Sections[1].End != 3 || state.Sections[1].CompileStatus != "compiled" {
		t.Fatalf("unexpected final partial section: %+v", state.Sections)
	}
	w = send("GET", "/sessions/"+state.ID+"/sections/2/files/mission.json", "")
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"schema_version":1`) {
		t.Fatalf("download = %d: %s", w.Code, w.Body)
	}
	w = send("GET", "/sessions/"+state.ID+"/sections/2/frames/0", "")
	if w.Code != 200 || w.Header().Get("Content-Type") != "image/jpeg" {
		t.Fatalf("sampled frame = %d: %s", w.Code, w.Header().Get("Content-Type"))
	}
	r.mu.Lock()
	history := r.rows[2].History
	r.mu.Unlock()
	if len(history) != 1 || history[0].Section != 1 {
		t.Fatalf("section 2 history = %+v", history)
	}
	s.Close()
	reopened := testService(t, root, nil, nil)
	recovered := waitStatus(t, reopened.runs[state.ID], "completed")
	if len(recovered.Sections) != 2 {
		t.Fatalf("lost the archived sections: %+v", recovered.Sections)
	}
}

func TestLivePreservesCaptureTimesAndFlushesPartialSection(t *testing.T) {
	if !mediaAvailable() {
		t.Skip("FFmpeg and ffprobe are required")
	}
	s := testService(t, t.TempDir(), nil, nil)
	r, err := s.start(startRequest{Source: "live", Producer: "alpha.front", Vehicle: "alpha"})
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.CommandContext(t.Context(), "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=96x64",
		"-frames:v", "1", "-c:v", "libx264", "-threads", "1", "-tune", "zerolatency", "-f", "h264", "pipe:1")
	packet, err := cmd.Output()
	if err != nil {
		t.Fatal(err)
	}
	anchor := time.Now().Add(-3 * time.Second)
	for i := range 3 {
		s.Ingest(EncodedFrame{Producer: "alpha.front", Vehicle: "alpha", Data: packet,
			CapturedAt: anchor.Add(time.Duration(i) * time.Second), ReceivedAt: time.Now(), ID: uint64(i), Keyframe: true})
	}
	deadline := time.After(4 * time.Second)
	for {
		state, changed := r.snap()
		if state.Collecting == 2 {
			break
		}
		select {
		case <-changed:
		case <-deadline:
			t.Fatal("second live section never started")
		}
	}
	if err := s.change(r, "stop"); err != nil {
		t.Fatal(err)
	}
	state := waitStatus(t, r, "stopped")
	if len(state.Sections) != 2 || state.Sections[0].CompileStatus != "compiled" || state.Sections[1].CompileStatus != "compiled" {
		t.Fatalf("live sections did not complete: %+v", state.Sections)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	first, second := r.rows[1], r.rows[2]
	if first.FrameCount != 2 || len(first.CaptureTimes) != 2 || first.CaptureTimes[0] == nil ||
		*first.CaptureTimes[0] != seconds(anchor) || second.FrameCount != 1 || second.InputQuality != "partial" {
		t.Fatalf("timestamps or trailing section lost: %+v %+v", first, second)
	}
}
