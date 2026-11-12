// Package aiconfig defines the non-secret AI settings of the GCS plugin.
package aiconfig

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/inferredmission"
)

type Options struct {
	ChatProvider      string                   `json:"chat_provider,omitempty"`
	ChatModel         string                   `json:"chat_model,omitempty"`
	ChatReasoning     string                   `json:"chat_reasoning_effort,omitempty"`
	ChatTemperature   *float64                 `json:"chat_temperature,omitempty"`
	VisionModel       string                   `json:"vision_model,omitempty"`
	VisionReasoning   string                   `json:"vision_reasoning_effort,omitempty"`
	VisionTemperature *float64                 `json:"vision_temperature,omitempty"`
	DataDir           string                   `json:"data_dir,omitempty"`
	InferredMission   inferredmission.Settings `json:"inferred_mission"`
}

func Defaults() Options {
	return Options{InferredMission: inferredmission.DefaultSettings()}
}

// Parse accepts only the documented AI fields. In particular the config
// cannot carry provider credentials or a provider endpoint.
func Parse(raw []byte) (Options, error) {
	if raw = bytes.TrimSpace(raw); len(raw) == 0 || raw[0] != '{' {
		return Options{}, errors.New("AI settings must be a JSON object")
	}
	type options Options
	opts := options(Defaults())
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&opts); err != nil {
		return Options{}, errors.New("invalid AI settings: use only documented fields and types; supply credentials via environment variables")
	}
	if err := dec.Decode(new(json.RawMessage)); !errors.Is(err, io.EOF) {
		return Options{}, errors.New("AI settings must contain a single JSON object")
	}
	if err := Options(opts).validate(); err != nil {
		return Options{}, err
	}
	return Options(opts), nil
}

func (o *Options) UnmarshalJSON(raw []byte) error {
	parsed, err := Parse(raw)
	if err == nil {
		*o = parsed
	}
	return err
}

func (o Options) validate() error {
	provider := strings.ToLower(o.ChatProvider)
	switch provider {
	case "", "openai", "anthropic":
	default:
		return errors.New("AI chat_provider must be openai or anthropic")
	}
	if provider == "anthropic" {
		if err := validateTemperature("chat_temperature", o.ChatTemperature); err != nil {
			return err
		}
		model := strings.ToLower(o.ChatModel)
		for _, prefix := range []string{"claude-opus-4-7", "claude-opus-4-8", "claude-opus-4.7", "claude-opus-4.8", "claude-opus-5", "claude-sonnet-5"} {
			if o.ChatTemperature != nil && strings.HasPrefix(model, prefix) {
				return errors.New("leave chat_temperature blank for this Anthropic model")
			}
		}
	} else if err := validateOpenAI("chat", o.ChatModel, o.ChatReasoning, o.ChatTemperature); err != nil {
		return err
	}
	if err := validateOpenAI("vision", o.VisionModel, o.VisionReasoning, o.VisionTemperature); err != nil {
		return err
	}
	return o.InferredMission.Validate()
}

func validateTemperature(field string, value *float64) error {
	if value != nil && (*value < 0 || *value > 1) {
		return fmt.Errorf("AI %s must be between 0 and 1", field)
	}
	return nil
}

func validateOpenAI(stage, model, effort string, temperature *float64) error {
	if err := validateTemperature(stage+"_temperature", temperature); err != nil {
		return err
	}
	model = strings.ToLower(model)
	if model == "" || strings.HasPrefix(model, "gpt-6.1-sol") {
		switch effort {
		case "", "low", "medium", "high", "xhigh", "max":
		default:
			return fmt.Errorf("AI %s_reasoning_effort for gpt-6.1-sol must be low, medium, high, xhigh or max", stage)
		}
	}
	if temperature == nil {
		return nil
	}
	if model == "" || strings.HasPrefix(model, "gpt-6") || (len(model) > 1 && model[0] == 'o' && model[1] >= '1' && model[1] <= '9') ||
		(strings.HasPrefix(model, "gpt-5") && effort != "none") {
		return fmt.Errorf("leave %s_temperature blank for the selected OpenAI reasoning model", stage)
	}
	return nil
}
