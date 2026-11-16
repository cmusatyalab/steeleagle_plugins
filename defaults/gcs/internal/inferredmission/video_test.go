package inferredmission

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/nl2dsl"
	"github.com/rs/zerolog"
)

type slidingCompiler struct{ videoCompiler }

func (slidingCompiler) CheckDraft(ctx context.Context, dsl string, plan mission.PlanningContext, check func(mission.Graph) []string) mission.Draft {
	return (videoCompiler{}).CheckDraft(ctx, dsl, plan, func(g mission.Graph) []string {
		if dsl == "wait ten" {
			g.Nodes[0].Params["Duration"] = 10
		}
		return check(g)
	})
}

func waitSignal[T any](t *testing.T, ch <-chan T) T {
	t.Helper()
	select {
	case value := <-ch:
		return value
	case <-time.After(15 * time.Second):
		t.Fatal("timed out waiting for the model request")
		var zero T
		return zero
	}
}

func TestSlidingMP4WorkersHistoryAndPause(t *testing.T) {
	if !mediaAvailable() {
		t.Skip("FFmpeg and ffprobe are required")
	}
	const assetID = "e5368c14-7e73-4a8c-a704-9db3228280e2"
	firstVision, middleVision, sixthVision, firstDSL := make(chan struct{}), make(chan struct{}), make(chan struct{}), make(chan struct{})
	entered := make(chan int, 20)
	generatingFirst := make(chan struct{}, 1)
	await := func(ctx context.Context, gate <-chan struct{}) bool {
		select {
		case <-gate:
			return true
		case <-ctx.Done():
			return false
		}
	}
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Instructions string          `json:"instructions"`
			Input        json.RawMessage `json:"input"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Errorf("decode model request: %v", err)
			w.WriteHeader(400)
			return
		}
		if strings.Contains(string(body.Input), "input_image") {
			i := strings.LastIndexByte(body.Instructions, '\n')
			var vision struct {
				Section int    `json:"section"`
				Mode    string `json:"history_mode"`
			}
			if err := json.Unmarshal([]byte(body.Instructions[i+1:]), &vision); err != nil || vision.Section == 0 {
				t.Errorf("read vision context: %v", err)
				w.WriteHeader(400)
				return
			}
			index, wait := vision.Section, 5
			if vision.Mode == "uniform" {
				index += 100
			}
			entered <- index
			var gate <-chan struct{}
			switch index {
			case 1:
				wait, gate = 10, firstVision
			case 3, 4, 5:
				gate = middleVision
			case 6:
				gate = sixthVision
			}
			if gate != nil && !await(r.Context(), gate) {
				return
			}
			fmt.Fprintf(w, `{"output":[{"type":"message","content":[{"type":"output_text","text":"Wait %d seconds, then return home."}]}]}`, wait)
			return
		}
		dsl := "wait five"
		if strings.Contains(string(body.Input), "Wait 10 seconds, then return home.") {
			dsl = "wait ten"
			generatingFirst <- struct{}{}
			if !await(r.Context(), firstDSL) {
				return
			}
		}
		fmt.Fprintf(w, `{"output":[{"type":"function_call","name":"submit_mission_dsl","arguments":"{\"dsl\":\"%s\",\"summary\":\"Wait then return.\"}"}]}`, dsl)
	}))
	t.Cleanup(model.Close)

	root := t.TempDir()
	cfg := DefaultSettings()
	cfg.SectionSeconds, cfg.SectionMode = 5, sectionSliding
	provider := nl2dsl.Provider{Name: "openai", ModelID: "test", Key: "test", BaseURL: model.URL}
	svc, err := New(t.Context(), root, cfg, provider, provider, nl2dsl.Generator{Model: provider, Compiler: slidingCompiler{}}, zerolog.Nop())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(svc.Close)

	clip := svc.videoPath(assetID)
	if err := os.Mkdir(filepath.Dir(clip), 0o700); err != nil {
		t.Fatal(err)
	}
	cmd := exec.CommandContext(t.Context(), "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=96x64:r=10:d=10.5", "-c:v", "libx264", "-threads", "1", clip)
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("create clip: %v: %s", err, output)
	}
	sum, err := digest(clip)
	if err != nil {
		t.Fatal(err)
	}
	asset := Video{ID: assetID, Name: "test.mp4", Duration: 10.5, SHA256: sum}
	if err := writeJSONFile(filepath.Join(filepath.Dir(clip), "asset.json"), asset); err != nil {
		t.Fatal(err)
	}
	start := func(history string) *run {
		t.Helper()
		r, err := svc.start(startRequest{Source: "uploaded_video", Asset: assetID, History: history})
		if err != nil {
			t.Fatal(err)
		}
		return r
	}
	setStatus := func(r *run, status string) {
		t.Helper()
		r.mu.Lock()
		defer r.mu.Unlock()
		if err := r.setStatus(status, ""); err != nil {
			t.Fatal(err)
		}
	}
	r := start("recency")
	// Wait for the first wave, including the one that completes immediately.
	seen := map[int]bool{}
	for range 5 {
		seen[waitSignal(t, entered)] = true
	}
	for index := 1; index <= 5; index++ {
		if !seen[index] {
			t.Fatalf("initial requests = %v, all five overlapping windows should start", seen)
		}
	}
	state, changed := r.snap()
	deadline := time.After(15 * time.Second)
	for len(state.Sections) < 6 {
		select {
		case <-changed:
			state, changed = r.snap()
		case <-deadline:
			t.Fatal("section 6 was never admitted after section 2 completed")
		}
	}
	if len(state.Sections) != 6 || state.Sections[5].Status != "preparing" || len(svc.videoSlots) != 5 {
		t.Fatalf("section 6 should occupy the fifth slot while waiting for section 1: %+v", state.Sections)
	}

	another := start("uniform")
	select {
	case index := <-entered:
		t.Fatalf("section %d bypassed history gating or the cross-run slot cap", index)
	case <-time.After(100 * time.Millisecond):
	}
	setStatus(another, "stopping")
	if stopped := waitStatus(t, another, "stopped"); len(stopped.Sections) != 0 {
		t.Fatalf("the waiting run started work despite the shared cap and stop: %+v", stopped.Sections)
	}

	close(firstVision)
	waitSignal(t, generatingFirst)
	if index := waitSignal(t, entered); index != 6 {
		t.Fatalf("section %d started while the first DSL and overlapping windows were in flight", index)
	}
	r.mu.Lock()
	first, sixth := r.rows[1], r.rows[6]
	r.mu.Unlock()
	if first.Status != "generating" || first.InferenceTime <= 0 || len(sixth.History) != 1 ||
		sixth.History[0].Section != 1 || sixth.History[0].Weight != 1 || sixth.HistoryWaitTime <= 0 {
		t.Fatalf("section 6 did not use section 1's vision before DSL was ready: first=%+v sixth=%+v", first.sectionSummary, sixth.History)
	}
	setStatus(r, "pausing")
	close(firstDSL)
	close(sixthVision)
	close(middleVision)
	paused := waitStatus(t, r, "paused")
	if len(paused.Sections) != 6 || paused.Settings.SectionMode != sectionSliding {
		t.Fatalf("pause should drain six started windows and retain their settings: %+v", paused)
	}
	for i, row := range paused.Sections {
		if row.Index != i+1 || row.Status != "completed" || row.CompileStatus != "compiled" || row.InferenceTime <= 0 || row.Elapsed < row.InferenceTime {
			t.Fatalf("out-of-order or incomplete timing/result: %+v", row)
		}
	}
	if err := svc.resume(r); err != nil {
		t.Fatal(err)
	}
	if index := waitSignal(t, entered); index != 7 {
		t.Fatalf("resumed section %d, want section 7", index)
	}
	completed := waitStatus(t, r, "completed")
	last := completed.Sections[len(completed.Sections)-1]
	if completed.Total != 7 || len(completed.Sections) != 7 || last.Start != 6 || last.End != 10.5 || completed.Updated <= paused.Updated {
		t.Fatalf("resumed analysis did not preserve geometry and advance the clock: %+v", completed)
	}
}

func TestSlidingShortMP4Window(t *testing.T) {
	cfg := DefaultSettings()
	cfg.SectionSeconds, cfg.SectionMode = 5, sectionSliding
	start, end := cfg.window(1, 2.5)
	if count := cfg.sectionCount(2.5); count != 1 || start != 0 || end != 2.5 {
		t.Fatalf("short clip should produce exactly [0, 2.5): count=%d window=[%v,%v)", count, start, end)
	}
}
