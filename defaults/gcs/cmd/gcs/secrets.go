package main

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// Keys are read from files in the daemon's files directory, never from the
// plugin args, which live in configs that are committed and shared.
const (
	filesEnv = "STEELEAGLE_FILES"
	// openAIKeyFile holds the OpenAI API key Inferred Mission and Chat use
	openAIKeyFile = ".openai"
	// mapboxKeyFile holds the Mapbox access token the web UI's maps use
	mapboxKeyFile = ".mapbox"
)

// secretPath is where the key file name is: in $STEELEAGLE_FILES.
func secretPath(name string) (string, error) {
	dir := os.Getenv(filesEnv)
	if dir == "" {
		return "", fmt.Errorf("$%s is not set, so %s can't be found", filesEnv, name)
	}
	return filepath.Join(dir, name), nil
}

// readSecret is the key in the file name in $STEELEAGLE_FILES, trimmed;
// "" with no error if the file isn't there.
func readSecret(name string) (string, string, error) {
	path, err := secretPath(name)
	if err != nil {
		return "", "", err
	}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return "", path, nil
	} else if err != nil {
		return "", path, fmt.Errorf("could not read %s: %w", path, err)
	}
	return strings.TrimSpace(string(data)), path, nil
}
