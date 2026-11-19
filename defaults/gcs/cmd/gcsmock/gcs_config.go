package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"

	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/aiconfig"
)

const maxAIConfigBytes = 64 << 10

// mockGCSConfig keeps the installed package and service configuration in sync
// with the arguments passed to the real GCS child on startup.
type mockGCSConfig struct {
	manifest []byte
	args     map[string]any
}

func loadGCSConfig(o options) (mockGCSConfig, error) {
	manifest, err := os.ReadFile(o.gcsManifest)
	if err != nil {
		return mockGCSConfig{}, fmt.Errorf("read GCS plugin manifest: %w", err)
	}
	plugin, err := config.ParsePluginConfigFromBytes(manifest)
	if err != nil {
		return mockGCSConfig{}, fmt.Errorf("parse GCS plugin manifest: %w", err)
	}
	if plugin.Plugin.Type != "engine" || plugin.Engine == nil {
		return mockGCSConfig{}, errors.New("GCS plugin manifest must declare an engine")
	}

	settings := mockGCSConfig{manifest: manifest, args: map[string]any{"http_addr": o.http}}
	if o.defaultFlow != "" {
		settings.args["default_flow"] = o.defaultFlow
		settings.args["default_flow_vehicle"] = o.defaultVehicle
	}
	if o.aiConfig != "" {
		ai, err := loadAIConfig(o.aiConfig)
		if err != nil {
			return mockGCSConfig{}, err
		}
		settings.args["ai"] = ai
	}
	return settings, nil
}

// loadAIConfig validates the file against the same settings used by GCS. Keep
// the original shape so Configure distinguishes explicit values from defaults.
func loadAIConfig(path string) (map[string]any, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("read -ai-config: %w", err)
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, maxAIConfigBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read -ai-config: %w", err)
	}
	if len(raw) > maxAIConfigBytes {
		return nil, errors.New("-ai-config must be at most 64 KiB")
	}
	if _, err := aiconfig.Parse(raw); err != nil {
		return nil, fmt.Errorf("invalid -ai-config: %w", err)
	}
	var ai map[string]any
	if err := json.Unmarshal(raw, &ai); err != nil || ai == nil {
		return nil, errors.New("-ai-config must contain one JSON object")
	}
	return ai, nil
}
