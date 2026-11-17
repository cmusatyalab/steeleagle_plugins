package mission

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"net/http"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/flows"
)

// PlanningContext is the operator's current V4 planning context. GeoJSON is
// the serialized Map tab data, in the same format as Graph.GeoJSON.
type PlanningContext struct {
	Vehicle   string          `json:"vehicle,omitempty"`
	Flow      *flows.Template `json:"flow,omitempty"`
	FlowIssue string          `json:"flow_issue,omitempty"`
	GeoJSON   string          `json:"geojson,omitempty"`
}

func (s *Service) planningTarget(ctx context.Context, plan PlanningContext) (BuildTarget, int, error) {
	target := BuildTarget{Key: "amd64", Arch: "amd64", Engines: map[string]string{}}
	if plan.Vehicle != "" {
		plugin, status, err := s.lookupVehiclePlugin(ctx, plan.Vehicle)
		if err != nil {
			return target, status, err
		}
		target.Plugin = plugin
	}
	return target, 0, nil
}

func (s *Service) resolvePlanningFlow(ctx context.Context, plan PlanningContext, g Graph, target BuildTarget) (BuildTarget, planningBindings, int, error) {
	bindings := driverBindings(target.Plugin)
	if plan.Flow.Empty() {
		return target, bindings, 0, nil
	}
	flow := *plan.Flow
	if plan.Vehicle != "" {
		if flow.Vehicle != "" && flow.Vehicle != plan.Vehicle {
			err := fmt.Errorf("the flow uses vehicle %q but Mission Builder uses %q", flow.Vehicle, plan.Vehicle)
			return target, bindings, http.StatusOK, err
		}
		flow.Vehicle = plan.Vehicle
	}
	if s.catalog == nil {
		return target, bindings, http.StatusServiceUnavailable, errors.New(noDaemon)
	}
	cat, err := s.catalog(ctx)
	if err != nil {
		return target, bindings, http.StatusServiceUnavailable, fmt.Errorf("Cannot list the flow catalog: %w", err)
	}
	res, err := s.resolveForValidate(cat, flow, g)
	if err != nil {
		return target, bindings, http.StatusOK, err
	}
	target.Flow, target.Manifest, target.Engines = res.Flow, res.Manifest, res.Engines
	bindings.addFlow(cat, flow, res)
	return target, bindings, 0, nil
}

func (s *Service) validationTarget(ctx context.Context, plan PlanningContext, g Graph) (BuildTarget, planningBindings, int, error) {
	target, status, err := s.planningTarget(ctx, plan)
	if err != nil {
		return target, planningBindings{}, status, err
	}
	return s.resolvePlanningFlow(ctx, plan, g, target)
}

func (s *Service) planningState(ctx context.Context, target BuildTarget) (*State, error) {
	if len(target.Plugin) == 0 {
		return s.Ready(ctx)
	}
	return s.scopedState(ctx, target.Plugin)
}

// DraftReference exposes the same schema and allowed imports used in Plan.
func (s *Service) DraftReference(ctx context.Context, plan PlanningContext) (map[string]any, error) {
	target, _, err := s.planningTarget(ctx, plan)
	if err != nil {
		return nil, err
	}
	st, err := s.planningState(ctx, target)
	if err != nil {
		return nil, err
	}
	_, bindings, _, flowErr := s.resolvePlanningFlow(ctx, plan, Graph{}, target)
	bindings = bindings.forRegistry(st.Index)
	context := map[string]any{
		"vehicle": plan.Vehicle, "flow": plan.Flow, "map_features": featureNames(plan.GeoJSON),
		"cameras": bindings.Cameras, "engines": bindings.Engines,
	}
	if flowErr != nil {
		context["flow_issue"] = flowErr.Error()
	} else if plan.FlowIssue != "" && plan.Flow.Empty() {
		context["flow_issue"] = plan.FlowIssue
	}
	header := s.header
	if _, ok := st.Index.Key("actions", climbActionType); ok {
		header = maps.Clone(header)
		addClimbImport(header)
	}
	ref := map[string]any{"schema": st.Schema, "imports": header["Imports"], "planning_context": context}
	for _, module := range s.cfg.Modules {
		if module.Path == sdkModulePath && module.Version != "" {
			ref["sdk_version"] = module.Version
			break
		}
	}
	return ref, nil
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
	target, _, err := s.planningTarget(ctx, plan)
	if err != nil {
		return failedDraft("compiler_unavailable", err.Error())
	}
	st, err := s.planningState(ctx, target)
	if err != nil {
		return failedDraft("compiler_unavailable", err.Error())
	}
	dsl, _ := UpgradeLegacyDSL(candidate)
	ast, err := ParseDSL(dsl)
	if err != nil {
		return failedDraft("parse_failed", err.Error())
	}
	// Relative aliases require a resolved flow. If the catalog or an engine is
	// unavailable, the context checks report it after the graph is imported.
	target, bindings, _, flowErr := s.resolvePlanningFlow(ctx, plan, Graph{}, target)
	bindings = bindings.forRegistry(st.Index)
	parsed := astToGraph(ast, st.Index, flowEngineAliases(plan.Flow, target.Engines), false)
	if warnings, ok := parsed["warnings"].([]any); ok && len(warnings) != 0 {
		result := failedDraft("invalid_graph")
		for _, warning := range warnings {
			result.Errors = append(result.Errors, fmt.Sprint(warning))
		}
		return result
	}
	g, err := graphFromParsed(parsed)
	if err != nil {
		return failedDraft("invalid_graph", err.Error())
	}
	g.GeoJSON = plan.GeoJSON
	if issues := StructuralErrors(g); len(issues) > 0 {
		return graphIssues("invalid_graph", issues)
	}
	bindTrackDraft(g, st.Index, bindings)
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

	if issues := draftBindingErrors(g, st.Index, plan, bindings, flowErr); len(issues) > 0 {
		result := graphIssues("needs_context", issues)
		result.DSL, result.Graph = normalized, &g
		return result
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

func graphFromParsed(parsed map[string]any) (Graph, error) {
	var g Graph
	encoded, err := json.Marshal(parsed)
	if err != nil {
		return g, err
	}
	dec := json.NewDecoder(bytes.NewReader(encoded))
	dec.UseNumber()
	err = dec.Decode(&g)
	return g, err
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
