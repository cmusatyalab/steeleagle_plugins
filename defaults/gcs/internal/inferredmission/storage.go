package inferredmission

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
)

var validID = regexp.MustCompile(`^[0-9a-f-]{36}$`)

func readJSON(path string, v any) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(data, v)
}

func writeJSONFile(path string, v any) error {
	f, err := os.CreateTemp(filepath.Dir(path), ".record-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	encErr := json.NewEncoder(f).Encode(v)
	closeErr := f.Close()
	if encErr != nil {
		return encErr
	}
	if closeErr != nil {
		return closeErr
	}
	return os.Rename(f.Name(), path)
}
