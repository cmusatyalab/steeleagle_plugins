package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestAIConfigRejectsNonSettings(t *testing.T) {
	for _, tc := range []struct {
		name string
		json string
	}{
		{"malformed", `{"chat_model":`},
		{"not an object", `null`},
		{"top-level credential", `{"OPENAI_API_KEY":"unexposed-test-value"}`},
		{"nested credential", `{"inferred_mission":{"api_key":"unexposed-test-value"}}`},
		{"wrong type", `{"inferred_mission":{"frame_fps":"two"}}`},
		{"trailing object", `{}{}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "ai.json")
			if err := os.WriteFile(path, []byte(tc.json), 0600); err != nil {
				t.Fatal(err)
			}
			if _, err := loadAIConfig(path); err == nil {
				t.Fatal("invalid AI settings were accepted")
			} else if strings.Contains(err.Error(), "unexposed-test-value") {
				t.Fatal("invalid settings error exposed the rejected value")
			}
		})
	}
}
