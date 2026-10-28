package mission

import (
	"bytes"
	"encoding/json"

	"github.com/cmusatyalab/steeleagle/sdk/dsl/compiler/parser"
)

// ParseDSL parses DSL text into eaglec AST JSON, decoded with numbers as
// json.Number. Syntax errors carry line and column positions.
func ParseDSL(dsl string) (map[string]any, error) {
	ast, err := parser.ParseFromSource([]byte(dsl))
	if err != nil {
		return nil, err
	}
	raw, err := astJson(ast)
	if err != nil {
		return nil, err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var out map[string]any
	if err := dec.Decode(&out); err != nil {
		return nil, err
	}
	return out, nil
}

// astJson and cleanAst are copied verbatim from eaglec
// (steeleagle sdk/cmd/eaglec/main.go:191-240), which doesn't export them.

// astJson encodes ast as JSON in the form parser.ParseFromJson reads: the
// Ast's fields under their Go names, without positions or absent values,
// and with String values unquoted (ParseFromJson quotes them again).
func astJson(ast *parser.Ast) ([]byte, error) {
	raw, err := json.Marshal(ast)
	if err != nil {
		return nil, err
	}
	// UseNumber keeps Int values exact, which float64 can't past 2^53
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var tree any
	if err := dec.Decode(&tree); err != nil {
		return nil, err
	}
	return json.Marshal(cleanAst(tree))
}

// cleanAst drops every Pos key and null value from a decoded Ast, and
// strips the quotes from the raw String tokens the parser captured.
func cleanAst(v any) any {
	switch v := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(v))
		for k, e := range v {
			if k == "Pos" || e == nil {
				continue
			}
			if s, ok := e.(string); ok && k == "String" && len(s) >= 2 {
				e = s[1 : len(s)-1]
			}
			out[k] = cleanAst(e)
		}
		return out
	case []any:
		for i, e := range v {
			v[i] = cleanAst(e)
		}
		return v
	}
	return v
}
