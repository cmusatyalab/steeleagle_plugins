package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/rs/zerolog"
)

func TestSecretsAreReadFromTheFilesDirectory(t *testing.T) {
	dir := t.TempDir()
	t.Setenv(filesEnv, dir)
	if key, path, err := readSecret(mapboxKeyFile); err != nil || key != "" || path != filepath.Join(dir, ".mapbox") {
		t.Fatalf("a missing key file = %q, %q, %v; want none", key, path, err)
	}
	if err := os.WriteFile(filepath.Join(dir, ".openai"), []byte("sk-test\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if key, _, err := readSecret(openAIKeyFile); err != nil || key != "sk-test" {
		t.Fatalf("got %q, %v", key, err)
	}
	t.Setenv(filesEnv, "")
	if _, _, err := readSecret(openAIKeyFile); err == nil {
		t.Fatal("found a key without a files directory")
	}
}

func TestHeadlessNeedsAnOpenAIKeyFile(t *testing.T) {
	dir := t.TempDir()
	t.Setenv(filesEnv, dir)
	err := runHeadless("10.0.0.1.inferred", "/nonexistent.sock", "", zerolog.Nop())
	if err == nil || !strings.Contains(err.Error(), filepath.Join(dir, ".openai")) {
		t.Fatalf("expected a complaint naming the key file, got %v", err)
	}
}

func TestHeadlessArgsSetTheInferenceSettings(t *testing.T) {
	a, err := parseHeadlessArgs(`{"vehicle": "drone", "camera": "front", "detector": "detection",
		"local_detector": "local.box_rerank", "verifier": "qwen3-vl:30b-a3b-instruct",
		"verifier_live_dir": "/root/live", "auto_deploy": true, "ai": {"inferred_mission": {"section_seconds": 10}}}`)
	if err != nil {
		t.Fatal(err)
	}
	s := a.AI.InferredMission
	if s.Detector != "detection" || s.LocalDetector != "local.box_rerank" || s.Verifier != "qwen3-vl:30b-a3b-instruct" ||
		s.VerifierLiveDir != "/root/live" || !s.AutoDeploy || s.SectionSeconds != 10 {
		t.Fatalf("got %+v", s)
	}
	if !a.camera("10.0.0.2.drone", "10.0.0.2.drone.front") || a.camera("10.0.0.2.drone", "10.0.0.2.drone.down") ||
		a.camera("10.0.0.2.other", "10.0.0.2.other.front") {
		t.Fatal("observed the wrong camera")
	}
	for _, invalid := range []string{`{"mapbox": "pk"}`, `{"verifier_live_dir": "relative"}`, `{"max_input_size": 0}`} {
		if _, err := parseHeadlessArgs(invalid); err == nil {
			t.Errorf("parseHeadlessArgs(%s) succeeded", invalid)
		}
	}
}
