package aiconfig

import (
	"strings"
	"testing"
)

func TestParseKeepsDefaultsAndExplicitZeroTemperature(t *testing.T) {
	opts, err := Parse([]byte(`{"chat_model":"gpt-4.1","chat_temperature":0,"inferred_mission":{"section_seconds":12}}`))
	if err != nil {
		t.Fatal(err)
	}
	if opts.ChatTemperature == nil || *opts.ChatTemperature != 0 || opts.InferredMission.SectionSeconds != 12 || opts.InferredMission.FrameFPS != 1 {
		t.Fatalf("wrong effective settings: %+v", opts)
	}
}

func TestParseRejectsSecretsAndInvalidOptions(t *testing.T) {
	cases := []struct {
		raw, reason string
	}{
		{`{"openai_api_key":"test-secret"}`, "credentials via environment"},
		{`{"inferred_mission":{"api_key":"test-secret"}}`, "credentials via environment"},
		{`{"vision_temperature":0.4}`, "leave vision_temperature blank"},
		{`{"chat_provider":"anthropic","chat_temperature":1.2}`, "between 0 and 1"},
		{`{"inferred_mission":{"frame_fps":1000}}`, "Invalid inferred mission settings"},
		{`{} {}`, "single JSON object"},
	}
	for _, tc := range cases {
		if _, err := Parse([]byte(tc.raw)); err == nil || !strings.Contains(err.Error(), tc.reason) || strings.Contains(err.Error(), "test-secret") {
			t.Fatalf("expected %q and no secret in error, got %v", tc.reason, err)
		}
	}
}
