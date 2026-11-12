package nl2dsl

import "testing"

func TestProviderTemperaturePayload(t *testing.T) {
	zero := 0.0
	p := Provider{ModelID: "test-model", Temperature: &zero}
	for _, makeRequest := range []func(Request) map[string]any{p.openAIRequest, p.anthropicRequest} {
		if got := makeRequest(Request{})["temperature"]; got != zero {
			t.Errorf("explicit zero temperature = %v", got)
		}
	}
	p.Temperature = nil
	for _, makeRequest := range []func(Request) map[string]any{p.openAIRequest, p.anthropicRequest} {
		if _, exists := makeRequest(Request{})["temperature"]; exists {
			t.Error("unset temperature should use the provider default")
		}
	}
}
