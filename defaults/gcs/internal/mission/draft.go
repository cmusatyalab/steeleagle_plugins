package mission

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/flows"
)

// PlanningContext is the operator's current V4 planning context. GeoJSON is
// the serialized Map tab data, in the same format as Graph.GeoJSON.
type PlanningContext struct {
	Vehicle string          `json:"vehicle,omitempty"`
	Flow    *flows.Template `json:"flow,omitempty"`
	GeoJSON string          `json:"geojson,omitempty"`
}

func (s *Service) validationTarget(ctx context.Context, plan PlanningContext, g Graph) (BuildTarget, int, error) {
	target := BuildTarget{Key: "amd64", Arch: "amd64", Engines: map[string]string{}}
	if plan.Vehicle != "" {
		plugin, status, err := s.lookupVehiclePlugin(ctx, plan.Vehicle)
		if err != nil {
			return target, status, err
		}
		target.Plugin = plugin
	}
	if plan.Flow.Empty() {
		return target, 0, nil
	}
	if s.catalog == nil {
		return target, http.StatusServiceUnavailable, errors.New(noDaemon)
	}
	cat, err := s.catalog(ctx)
	if err != nil {
		return target, http.StatusServiceUnavailable, fmt.Errorf("Cannot list the flow catalog: %w", err)
	}
	res, err := s.resolveForValidate(cat, *plan.Flow, g)
	if err != nil {
		return target, http.StatusOK, err
	}
	target.Flow, target.Manifest, target.Engines = res.Flow, res.Manifest, res.Engines
	return target, 0, nil
}

func (s *Service) draftState(ctx context.Context, vehicle string) (*State, error) {
	st, err := s.Ready(ctx)
	if err != nil || vehicle == "" {
		return st, err
	}
	plugin, _, err := s.lookupVehiclePlugin(ctx, vehicle)
	if err != nil {
		return nil, err
	}
	return s.scopedState(ctx, plugin)
}

// DraftReference exposes the same schema and allowed imports used in Plan.
func (s *Service) DraftReference(ctx context.Context, plan PlanningContext) (map[string]any, error) {
	st, err := s.draftState(ctx, plan.Vehicle)
	if err != nil {
		return nil, err
	}
	return map[string]any{"schema": st.Schema, "imports": s.header["Imports"], "planning_context": map[string]any{
		"vehicle": plan.Vehicle, "flow": plan.Flow, "map_features": featureNames(plan.GeoJSON),
	}}, nil
}

type Draft struct {
	DSL    string   `json:"normalized_dsl,omitempty"`
	Graph  *Graph   `json:"graph,omitempty"`
	Status string   `json:"status"`
	Errors []string `json:"errors"`
}

// CheckDraft imports the candidate with the V4 parser and compiles it through
// the same validation target as the Plan page. The caller can add checks for
// requirements inferred from video before an otherwise valid graph is built.
func (s *Service) CheckDraft(ctx context.Context, candidate string, plan PlanningContext, check func(Graph) []string) Draft {
	st, err := s.draftState(ctx, plan.Vehicle)
	if err != nil {
		return failedDraft("compiler_unavailable", err.Error())
	}
	dsl, _ := UpgradeLegacyDSL(candidate)
	ast, err := ParseDSL(dsl)
	if err != nil {
		return failedDraft("parse_failed", err.Error())
	}
	parsed := astToGraph(ast, st.Index, draftEngineAliases(plan.Flow))
	if warnings, ok := parsed["warnings"].([]any); ok && len(warnings) != 0 {
		result := failedDraft("invalid_graph")
		for _, warning := range warnings {
			result.Errors = append(result.Errors, fmt.Sprint(warning))
		}
		return result
	}
	encoded, err := json.Marshal(parsed)
	if err != nil {
		return failedDraft("invalid_graph", err.Error())
	}
	var g Graph
	dec := json.NewDecoder(bytes.NewReader(encoded))
	dec.UseNumber()
	if err := dec.Decode(&g); err != nil {
		return failedDraft("invalid_graph", err.Error())
	}
	g.GeoJSON = plan.GeoJSON
	if issues := StructuralErrors(g); len(issues) > 0 {
		return graphIssues("invalid_graph", issues)
	}
	if check != nil {
		if issues := check(g); len(issues) > 0 {
			return failedDraft("task_mismatch", issues...)
		}
	}
	// Local Go module overrides belong to the compiler configuration.
	canonical, err := graphToPortableAst(g, st.Index, map[string]any{"Imports": s.header["Imports"]})
	if err != nil {
		return failedDraft("invalid_graph", err.Error())
	}
	normalized, err := AstToDSL(canonical)
	if err != nil {
		return failedDraft("invalid_graph", err.Error())
	}

	if missing := missingDraftContext(g, st.Index, plan); len(missing) > 0 {
		return Draft{DSL: normalized, Graph: &g, Status: "needs_context", Errors: missing}
	}
	target, _, err := s.validationTarget(ctx, plan, g)
	if err != nil {
		return Draft{DSL: normalized, Graph: &g, Status: "needs_context", Errors: []string{err.Error()}}
	}
	_, issues, err := s.CompileGraph(ctx, g, []BuildTarget{target})
	if err != nil {
		return failedDraft("compiler_unavailable", err.Error())
	}
	if len(issues) > 0 {
		return graphIssues("compile_failed", issues)
	}
	return Draft{DSL: normalized, Graph: &g, Status: "compiled", Errors: []string{}}
}

// An older draft may use an SDK-style identifier. Restore the original
// template name only when the selected flow has one unambiguous match.
func draftEngineAliases(flow *flows.Template) map[string]string {
	if flow == nil {
		return nil
	}
	aliases := make(map[string]string, len(flow.Engines))
	for _, engine := range flow.Engines {
		alias := constName(engine.Name)
		if existing, ok := aliases[alias]; ok && existing != engine.Name {
			aliases[alias] = ""
		} else {
			aliases[alias] = engine.Name
		}
	}
	return aliases
}

func failedDraft(status string, messages ...string) Draft {
	return Draft{Status: status, Errors: messages}
}

func graphIssues(status string, issues []GraphError) Draft {
	result := failedDraft(status)
	for _, issue := range issues {
		result.Errors = append(result.Errors, issue.Message)
	}
	return result
}

func featureNames(geojson string) map[string]string {
	var collection struct {
		Features []struct {
			Properties struct {
				Name string `json:"name"`
			} `json:"properties"`
			Geometry struct {
				Type string `json:"type"`
			} `json:"geometry"`
		} `json:"features"`
	}
	features := map[string]string{}
	if json.Unmarshal([]byte(geojson), &collection) == nil {
		for _, feature := range collection.Features {
			if feature.Properties.Name != "" {
				features[feature.Properties.Name] = feature.Geometry.Type
			}
		}
	}
	return features
}

func missingDraftContext(g Graph, ix *TypeIndex, plan PlanningContext) []string {
	var missing []string
	features := featureNames(plan.GeoJSON)
	engines := map[string]bool{}
	if plan.Flow != nil {
		for _, engine := range plan.Flow.Engines {
			engines[engine.Name] = true
		}
	}
	check := func(section string, node GraphNode) {
		qualified, _ := ix.Qualify(section, node.TypeName)
		for name, raw := range node.Params {
			goType, _ := ix.FieldType(qualified, name)
			value, _ := raw.(string)
			switch {
			case isFeatureType(goType) && features[value] != Bare(goType):
				missing = append(missing, fmt.Sprintf("%s: select a %s in the Map tab for %s (%q).", node.InstanceID, Bare(goType), name, value))
			case isEngineType(goType) && !engines[value]:
				missing = append(missing, fmt.Sprintf("%s: bind %s (%q) in the Flow Builder.", node.InstanceID, name, value))
			case goType == "params.Camera" && strings.HasPrefix(value, "UNBOUND_"):
				missing = append(missing, fmt.Sprintf("%s: select the camera in the Mission Builder.", node.InstanceID))
			}
		}
	}
	for _, node := range g.Nodes {
		check("actions", node)
	}
	for _, event := range g.Events {
		check("events", GraphNode(event))
	}
	return missing
}
