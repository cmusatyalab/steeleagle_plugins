package daemons

import (
	"errors"
	"fmt"
	"slices"
	"strings"

	"github.com/BurntSushi/toml"
)

// inspectConfigCredentials returns a document with legacy GCS credentials
// removed. Inspect the untyped document before daemon validation so the same
// check covers raw TOML and rendered forms, even when a service is invalid.
func inspectConfigCredentials(raw []byte) (map[string]any, []string, error) {
	var document map[string]any
	if _, err := toml.Decode(string(raw), &document); err != nil {
		return nil, nil, safeTOMLError(err)
	}
	keys := map[string]bool{}
	for name, services := range document {
		if strings.EqualFold(name, "services") {
			visitTables(services, func(service map[string]any) { removeServiceCredentials(service, keys) })
		}
	}
	issues := make([]string, 0, len(keys))
	for key := range keys {
		issues = append(issues, fmt.Sprintf("Remove %s from the GCS plugin arguments and use API credentials under GCS AI settings.", key))
	}
	slices.Sort(issues)
	return document, issues, nil
}

func removeServiceCredentials(service map[string]any, keys map[string]bool) {
	for name, value := range service {
		if !strings.EqualFold(name, "plugin") {
			continue
		}
		plugin, _ := value.(map[string]any)
		if !isGCSPlugin(plugin) {
			continue
		}
		for name, args := range plugin {
			if strings.EqualFold(name, "args") {
				visitTables(args, func(table map[string]any) { removeCredentialArgs(table, keys) })
			}
		}
	}
}

func isGCSPlugin(plugin map[string]any) bool {
	for name, value := range plugin {
		if strings.EqualFold(name, "plugin") && value == "gcs" {
			return true
		}
	}
	return false
}

func removeCredentialArgs(table map[string]any, keys map[string]bool) {
	for name, value := range table {
		if key := credentialArg(name); key != "" {
			keys[key] = true
			delete(table, name)
			continue
		}
		visitTables(value, func(nested map[string]any) { removeCredentialArgs(nested, keys) })
	}
}

// TOML arrays of tables and inline arrays have different decoded Go types.
func visitTables(value any, visit func(map[string]any)) {
	switch v := value.(type) {
	case map[string]any:
		visit(v)
	case []map[string]any:
		for _, t := range v {
			visit(t)
		}
	case []any:
		for _, entry := range v {
			visitTables(entry, visit)
		}
	}
}

var credentialKeySeparators = strings.NewReplacer("_", "", "-", "", " ", "", ".", "")

func credentialArg(name string) string {
	switch credentialKeySeparators.Replace(strings.ToLower(strings.TrimSpace(name))) {
	case "openaiapikey":
		return "OPENAI_API_KEY"
	case "anthropicapikey":
		return "ANTHROPIC_API_KEY"
	case "openaiaiapi":
		return "OPENAI_AI_API"
	case "apikey":
		return "api_key"
	default:
		return ""
	}
}

// TOML parser diagnostics can embed the original token or value. Keep the
// location but never include those excerpts in an HTTP response.
func safeTOMLError(err error) *TOMLError {
	result := &TOMLError{Message: "Invalid TOML. Check the key, value, and quoting near this location."}
	var parseErr toml.ParseError
	if errors.As(err, &parseErr) {
		result.Line, result.Column = parseErr.Position.Line, parseErr.Position.Col
	}
	return result
}
