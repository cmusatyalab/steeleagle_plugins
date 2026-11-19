package flows

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
)

// DefaultFlow is the flow the Flow Builder starts with (the GCS's
// default_flow arg): a flow config TOML file whose names are relative to
// the vehicle it is for (vehicle.rgb, local.box_rerank, remote.detection),
// as a vehicle's own flows are and the Flow Builder exports them.
// Vehicle names that vehicle (bare or full name); empty for the one the Flow
// Builder has selected, else the first running one. Path is read again for
// every request, so an edited file is picked up. Started identifies this run
// of the GCS (e.g. its start time), so each run's default flow is loaded
// again, even if the file is the same as the last run's.
type DefaultFlow struct {
	Path    string
	Vehicle string
	Started string
}

// defaultFlowResponse is GET /api/flows/default's body. Version changes
// whenever the file does or the GCS restarts (an experiment's trial restarts
// it with its config), so the Flow Builder applies each version once and
// keeps the user's edits to it until it changes.
type defaultFlowResponse struct {
	Configured bool   `json:"configured"`
	Name       string `json:"name,omitempty"`
	Vehicle    string `json:"vehicle,omitempty"`
	TOML       string `json:"toml,omitempty"`
	Version    string `json:"version,omitempty"`
}

// ServeHTTP serves GET /api/flows/default: {"configured": false} without a
// default flow; else its TOML, name, vehicle and version. A file that can't
// be read or isn't a flow config is a 422 with the reason as its detail.
func (d DefaultFlow) ServeHTTP(w http.ResponseWriter, _ *http.Request) {
	if d.Path == "" {
		writeJSON(w, http.StatusOK, defaultFlowResponse{})
		return
	}
	_, data, err := d.load()
	if err != nil {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "default flow " + d.Path + ": " + err.Error()})
		return
	}
	sum := sha256.Sum256(data)
	writeJSON(w, http.StatusOK, defaultFlowResponse{
		Configured: true,
		Name:       strings.TrimSuffix(filepath.Base(d.Path), filepath.Ext(d.Path)),
		Vehicle:    d.Vehicle,
		TOML:       string(data),
		Version:    hex.EncodeToString(sum[:8]) + "-" + d.Started,
	})
}

// load reads and parses the default flow's file.
func (d DefaultFlow) load() (Template, []byte, error) {
	data, err := os.ReadFile(d.Path)
	if err != nil {
		return Template{}, nil, err
	}
	t, err := ParseTemplateTOML(data)
	if err == nil && t.Empty() {
		err = errors.New("no producers or engines in this flow")
	}
	return t, data, err
}

// For is the default flow for planning vehicle's (full name) missions,
// translated for it and the orchestrator at orchestratorIP from the file as
// it is now. ok is false without a default flow or if it is for another
// vehicle.
func (d DefaultFlow) For(c Catalog, vehicle, orchestratorIP string) (t Template, ok bool, err error) {
	if d.Path == "" {
		return Template{}, false, nil
	}
	if d.Vehicle != "" && d.Vehicle != vehicle {
		if _, bare := fleet.SplitName(vehicle); bare != d.Vehicle {
			return Template{}, false, nil
		}
	}
	t, _, err = d.load()
	if err != nil {
		return Template{}, true, fmt.Errorf("default flow %s: %w", d.Path, err)
	}
	t, err = t.ForVehicle(c, vehicle, orchestratorIP)
	if err != nil {
		return Template{}, true, fmt.Errorf("default flow %s: %w", d.Path, err)
	}
	return t, true, nil
}
