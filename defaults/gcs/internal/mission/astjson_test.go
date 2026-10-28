package mission

import (
	"os"
	"testing"
)

func TestParseDSLMatchesExampleAst(t *testing.T) {
	got, err := ParseDSL(exDSL)
	if err != nil {
		t.Fatal(err)
	}
	// The parser writes absent stanzas and fields as empty values where the
	// example leaves them out, so compare with empty values pruned
	eq(t, prune(normalize(t, got)), prune(normalize(t, exAST(t))))
}

// prune drops empty strings, lists and objects, recursively.
func prune(v any) any {
	switch v := v.(type) {
	case map[string]any:
		out := map[string]any{}
		for k, e := range v {
			if e = prune(e); e != nil {
				out[k] = e
			}
		}
		if len(out) == 0 {
			return nil
		}
		return out
	case []any:
		var out []any
		for _, e := range v {
			out = append(out, prune(e))
		}
		if len(out) == 0 {
			return nil
		}
		return out
	case string:
		if v == "" {
			return nil
		}
	}
	return v
}

func TestParseDSLLegacyFixture(t *testing.T) {
	raw, err := os.ReadFile("testdata/legacy_import.dsl")
	if err != nil {
		t.Fatal(err)
	}
	upgraded, warnings := UpgradeLegacyDSL(string(raw))
	eq(t, warnings, []string{legacyWarning})
	if _, err := ParseDSL(upgraded); err != nil {
		t.Fatal(err)
	}
}

func TestParseDSLSyntaxError(t *testing.T) {
	if _, err := ParseDSL("Actions:\n    this is (not valid"); err == nil {
		t.Fatal("expected a syntax error")
	}
}
