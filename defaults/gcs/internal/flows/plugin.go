// Package flows builds what the Flow Builder needs from the daemon: the
// catalog of the vehicles and engines on each daemon, with the streams the
// vehicles produce and the types the engines accept and emit.
package flows

import (
	"errors"
	"fmt"

	"github.com/cmusatyalab/steeleagle/config"
)

const (
	// TelemetryType is the type of the telemetry stream every vehicle has.
	TelemetryType = "steeleagle_protocol/v1/messages/telemetry/Telemetry"
	// ResultLabel is the label of an engine's result event.
	ResultLabel = "result"
)

// Stream is something a vehicle produces: its telemetry, or one of its cameras.
type Stream struct {
	Name        string `json:"name"`
	Kind        string `json:"kind"` // "telemetry" or "camera"
	Type        string `json:"type"`
	Primary     bool   `json:"primary"`
	Description string `json:"description,omitempty"`
	HRes        *int   `json:"hres,omitempty"`
	VRes        *int   `json:"vres,omitempty"`
	FPS         *int   `json:"fps,omitempty"`
}

// Event is something an engine emits.
type Event struct {
	Label string  `json:"label"`
	Type  *string `json:"type"`
}

// Engine is an engine service. Name is its full service name and Bare the
// part after its daemon's IP.
type Engine struct {
	Name     string   `json:"name"`
	Bare     string   `json:"bare"`
	Category *string  `json:"category"`
	Accepts  []string `json:"accepts"`
	Result   string   `json:"result"`
	Events   []Event  `json:"events"`
	Typed    bool     `json:"typed"`
	Problem  string   `json:"problem,omitempty"`
}

// telemetryStream is the stream every vehicle has, whatever its driver declares.
func telemetryStream() Stream {
	return Stream{Name: "telemetry", Kind: "telemetry", Type: TelemetryType}
}

// optInt returns a pointer to v, or nil for zero (not declared).
func optInt(v uint32) *int {
	if v == 0 {
		return nil
	}
	n := int(v)
	return &n
}

// ParseDriver reads a driver's plugin config. The streams are the telemetry
// stream followed by one per camera. A camera that can't be used is skipped
// and described in problems. It returns an error if conf has no driver cap.
func ParseDriver(conf []byte) (streams []Stream, problems []string, err error) {
	p, err := config.ParsePluginConfigFromBytes(conf)
	if err != nil {
		return nil, nil, fmt.Errorf("plugin TOML is invalid: %w", err)
	}
	if p.Driver == nil {
		return nil, nil, errors.New("plugin file has no [driver] table")
	}
	streams = []Stream{telemetryStream()}
	for _, c := range p.Driver.Cameras {
		switch {
		case c.Name == "":
			problems = append(problems, "a camera has no name; skipped")
		case c.Type == "":
			problems = append(problems, fmt.Sprintf("camera %q has no type; skipped", c.Name))
		case hasStream(streams, c.Name):
			problems = append(problems, fmt.Sprintf("camera %q duplicates the %s stream; skipped", c.Name, c.Name))
		default:
			streams = append(streams, Stream{
				Name: c.Name, Kind: "camera", Type: c.Type, Primary: c.Primary,
				Description: c.Description,
				HRes:        optInt(c.HRes), VRes: optInt(c.VRes), FPS: optInt(c.Fps),
			})
		}
	}
	return streams, problems, nil
}

func hasStream(streams []Stream, name string) bool {
	for _, s := range streams {
		if s.Name == name {
			return true
		}
	}
	return false
}

// ParseEngine reads an engine's plugin config. It fills everything but Name
// and Bare. The engine's only event is its result. It returns an error if
// conf has no engine cap or the cap accepts nothing.
func ParseEngine(conf []byte) (Engine, error) {
	p, err := config.ParsePluginConfigFromBytes(conf)
	if err != nil {
		return Engine{}, fmt.Errorf("plugin TOML is invalid: %w", err)
	}
	if p.Engine == nil {
		return Engine{}, errors.New("plugin file has no [engine] table")
	}
	e := p.Engine
	if len(e.Inputs) == 0 {
		return Engine{}, errors.New("[engine] inputs must be a list of message types")
	}
	for _, in := range e.Inputs {
		if in == "" {
			return Engine{}, errors.New("[engine] inputs must be a list of message types")
		}
	}
	out := Engine{
		Accepts: append([]string(nil), e.Inputs...),
		Result:  e.Result,
		Events:  []Event{resultEvent(e.Result)},
		Typed:   true,
	}
	if e.Type != "" {
		t := e.Type
		out.Category = &t
	}
	return out, nil
}

func resultEvent(result string) Event {
	if result == "" {
		return Event{Label: ResultLabel}
	}
	return Event{Label: ResultLabel, Type: &result}
}

// untypedEngine is an engine whose plugin config can't be read: it accepts
// anything and emits an untyped result.
func untypedEngine(problem string) Engine {
	return Engine{
		Accepts: []string{"*"}, Events: []Event{resultEvent("")},
		Problem: problem,
	}
}
