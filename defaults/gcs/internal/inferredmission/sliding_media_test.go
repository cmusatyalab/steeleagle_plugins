package inferredmission

import (
	"bytes"
	"math"
	"os/exec"
	"path/filepath"
	"slices"
	"testing"
	"time"
)

func TestSlidingVideoPreservesGlobalSamples(t *testing.T) {
	if !mediaAvailable() {
		t.Skip("FFmpeg and ffprobe are required")
	}
	root := t.TempDir()
	path := filepath.Join(root, "sliding.mp4")
	cmd := exec.CommandContext(t.Context(), "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
		"testsrc2=s=96x64:r=30000/1001:d=7.3", "-c:v", "libx264", "-threads", "1", path)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("Create test clip: %v: %s", err, out)
	}
	cfg := DefaultSettings()
	cfg.SectionSeconds = 5
	cfg.MaxFrameSide = 96
	cfg.SectionMode = sectionSliding
	for _, test := range []struct {
		name string
		fps  float64
	}{
		{"one frame per second", 1},
		{"window starts in the middle of a slot", 1.5},
	} {
		t.Run(test.name, func(t *testing.T) {
			cfg := cfg
			cfg.FrameFPS = test.fps
			root := t.TempDir()
			first, err := sampleVideo(t.Context(), path, filepath.Join(root, "first"), 0, 5, cfg)
			if err != nil {
				t.Fatal(err)
			}
			second, err := sampleVideo(t.Context(), path, filepath.Join(root, "second"), 1, 6, cfg)
			if err != nil {
				t.Fatal(err)
			}
			start := slices.IndexFunc(first, func(f sample) bool { return f.Time >= 1 })
			end := slices.IndexFunc(second, func(f sample) bool { return f.Time >= 4 })
			if start < 1 || end < 1 || len(first)-start != end {
				t.Fatalf("Shared samples were not found on both timelines: %d, %d", len(first)-start, end)
			}
			if test.fps == 1 && (len(first) != 5 || len(second) != 5 || end != 4) {
				t.Fatalf("Expected 5 frames and 4 shared: %d, %d, %d", len(first), len(second), end)
			}
			for i, sample := range second[:end] {
				prior := first[start+i]
				if math.Abs((prior.Time-1)-sample.Time) > 0.00001 || !bytes.Equal(prior.JPEG, sample.JPEG) {
					t.Fatalf("Sample %d changed between windows: %.6f vs %.6f (section times)", i, prior.Time, sample.Time)
				}
			}
		})
	}
}

func TestSlidingLiveWindowsReuseSamplesAndRunSerially(t *testing.T) {
	if !mediaAvailable() {
		t.Skip("FFmpeg and ffprobe are required")
	}
	block, entered := make(chan struct{}), make(chan struct{})
	s := testService(t, t.TempDir(), block, entered)
	s.settings.SectionSeconds = 5
	s.settings.SectionMode = sectionSliding
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
	anchor := time.Now().Add(-7 * time.Second)
	for i, elapsed := range []time.Duration{
		0, 1050 * time.Millisecond, 2 * time.Second, 3050 * time.Millisecond, 4 * time.Second, 5050 * time.Millisecond,
	} {
		s.Ingest(EncodedFrame{Producer: "alpha.front", Vehicle: "alpha", Data: packet,
			CapturedAt: anchor.Add(elapsed), ReceivedAt: time.Now(), ID: uint64(i + 1), Keyframe: true})
	}
	deadline := time.After(5 * time.Second)
	select {
	case <-entered:
	case <-deadline:
		t.Fatal("First section did not reach vision")
	}
	for {
		state, changed := r.snap()
		if len(state.Sections) == 2 {
			if state.Sections[1].Status != "queued" {
				t.Fatalf("Second live section ran before the first finished: %+v", state.Sections)
			}
			break
		}
		select {
		case <-changed:
		case <-deadline:
			t.Fatal("Second sliding section did not queue after the sixth sample")
		}
	}
	if err := s.change(r, "stop"); err != nil {
		t.Fatal(err)
	}
	close(block)
	state := waitStatus(t, r, "stopped")
	if len(state.Sections) != 2 {
		t.Fatalf("Stopping generated an overlap-only window: %+v", state.Sections)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	for i, expectedTimes := range [][]float64{{0, 1.05, 2, 3.05, 4}, {0.05, 1, 2.05, 3, 4.05}} {
		row := r.rows[i+1]
		if row.CompileStatus != "compiled" || row.Start != float64(i) || row.End != float64(i+5) ||
			row.FrameCount != 5 || len(row.FrameIDs) != 5 || !slices.Equal(row.FrameTimes, expectedTimes) {
			t.Fatalf("Section %d did not retain its exact window and frame times: %+v", i+1, row)
		}
		for j, id := range row.FrameIDs {
			if id == nil || *id != uint64(i+j+1) {
				t.Fatalf("Section %d does not share the expected frame at offset %d", i+1, j)
			}
		}
	}
}
