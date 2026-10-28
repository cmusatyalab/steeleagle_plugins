package mission

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/flows"
	"github.com/rs/zerolog"
)

// maxBodySize bounds the JSON bodies the compiler routes accept.
const maxBodySize = 32 << 20

// noDaemon is the detail of a 503 for a route that needs the daemon.
const noDaemon = "the GCS has no connection to the daemon"

// validateMissionName is the manifest's mission name in a Validate build.
const validateMissionName = "validate"

// CatalogFunc lists the flow catalog from the daemon.
type CatalogFunc func(ctx context.Context) (flows.Catalog, error)

// VehiclesFunc lists the running vehicles.
type VehiclesFunc func(ctx context.Context) ([]fleet.Vehicle, error)

// State is the loaded compiler state: the type index and the /api/schema body.
type State struct {
	Index  *TypeIndex
	Schema map[string]any
}

// Service serves the Plan page's compiler routes.
type Service struct {
	c      Compiler
	cfg    CompilerConfig
	log    zerolog.Logger
	header map[string]any
	// gcsIP is the IP of the GCS's own daemon, the orchestrator.
	gcsIP string
	// catalog lists the flow catalog; nil without a daemon.
	catalog CatalogFunc
	// vehicles lists the running vehicles; nil without a daemon.
	vehicles VehiclesFunc

	ready chan struct{} // closed when the first load has finished

	mu    sync.RWMutex
	state *State
	err   error
	// scoped caches the state scrubbed by each driver plugin config, keyed
	// by the config.
	scoped map[string]*State
}

// BuildTarget is a binary CompileGraph builds. Key names the target in
// results and error messages.
type BuildTarget struct {
	Key    string
	Arch   string
	Plugin []byte
	// Flow and Manifest are the resolved flow and manifest TOML, nil
	// without a flow.
	Flow     []byte
	Manifest []byte
	// Engines maps the engine names the mission uses to their full names
	// (see GraphToAst): nil uses them as written, and an empty map allows
	// none.
	Engines map[string]string
}

// NewService returns a Service compiling with c. cfg should be resolved
// (ResolveConfig). gcsIP is the IP of the GCS's own daemon, which
// orchestrates flows, and catalog and vehicles list the flow catalog and the
// running vehicles (nil without a daemon). The type registry is loaded in the
// background; routes wait for it.
func NewService(c Compiler, cfg CompilerConfig, gcsIP string, catalog CatalogFunc, vehicles VehiclesFunc, log zerolog.Logger) *Service {
	s := &Service{
		c: c, cfg: cfg, log: log, header: cfg.Header(), gcsIP: gcsIP, catalog: catalog, vehicles: vehicles,
		ready: make(chan struct{}), scoped: map[string]*State{},
	}
	go func() {
		defer close(s.ready)
		st, err := s.load(context.Background(), nil)
		s.mu.Lock()
		s.state, s.err = st, err
		s.mu.Unlock()
		if err != nil {
			log.Error().Err(err).Msg("FSM compiler unavailable")
			return
		}
		log.Info().Int("actions", len(st.Schema["actions"].(map[string]any))).
			Int("events", len(st.Schema["events"].(map[string]any))).Msg("FSM compiler ready")
	}()
	return s
}

// load loads the type registry scrubbed by plugin's driver cap, or unscrubbed
// if plugin is nil.
func (s *Service) load(ctx context.Context, plugin []byte) (*State, error) {
	reg, err := s.c.Registry(ctx, plugin)
	if err != nil {
		return nil, err
	}
	ix := NewTypeIndex(reg)
	schema := RegistryToSchema(ix)
	imports := []any{}
	for _, i := range s.cfg.Imports {
		imports = append(imports, map[string]any{"path": i.Path, "alias": i.Alias})
	}
	schema["imports"] = imports
	return &State{Index: ix, Schema: schema}, nil
}

// Ready returns the loaded compiler state, waiting for the background load.
// Its error reads "FSM compiler unavailable: <reason>".
func (s *Service) Ready(ctx context.Context) (*State, error) {
	select {
	case <-s.ready:
	case <-ctx.Done():
		return nil, fmt.Errorf("FSM compiler unavailable: %w", ctx.Err())
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.err != nil {
		return nil, fmt.Errorf("FSM compiler unavailable: %w", s.err)
	}
	return s.state, nil
}

// scopedState returns the state scrubbed by plugin's driver cap, loading it
// on first use.
func (s *Service) scopedState(ctx context.Context, plugin []byte) (*State, error) {
	key := string(plugin)
	s.mu.RLock()
	st, ok := s.scoped[key]
	s.mu.RUnlock()
	if ok {
		return st, nil
	}
	st, err := s.load(ctx, plugin)
	if err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.scoped[key] = st
	s.mu.Unlock()
	return st, nil
}

// vehiclePlugin returns the plugin config of the named running vehicle, or
// writes an error and returns false.
func (s *Service) vehiclePlugin(w http.ResponseWriter, r *http.Request, name string) ([]byte, bool) {
	if s.vehicles == nil {
		writeError(w, http.StatusServiceUnavailable, noDaemon)
		return nil, false
	}
	vs, err := s.vehicles(r.Context())
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "Cannot list vehicles: "+err.Error())
		return nil, false
	}
	for _, v := range vs {
		if v.Name != name {
			continue
		}
		if v.Driver == nil {
			writeError(w, http.StatusUnprocessableEntity, fmt.Sprintf("vehicle %s has no driver config: %v", name, v.DriverErr))
			return nil, false
		}
		return v.Config, true
	}
	writeError(w, http.StatusNotFound, fmt.Sprintf("vehicle %s is not running", name))
	return nil, false
}

// graphRequest is a mission graph as the frontend posts it; unknown fields
// are ignored. Vehicle, if set, is the vehicle whose driver config the
// mission is compiled with.
type graphRequest struct {
	Graph
	Arch    string          `json:"arch"`
	Flow    *flows.Template `json:"flow"`
	Vehicle string          `json:"vehicle"`
}

func decodeBody(w http.ResponseWriter, r *http.Request, v any) bool {
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxBodySize))
	dec.UseNumber()
	if err := dec.Decode(v); err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid request: "+err.Error())
		return false
	}
	return true
}

// ready503 returns the ready state, or writes a 503 and returns nil.
func (s *Service) ready503(w http.ResponseWriter, r *http.Request) *State {
	st, err := s.Ready(r.Context())
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return nil
	}
	return st
}

// ServeSchema serves GET /api/schema; ?refresh=1 reloads the registry, and
// ?vehicle=<name> gives the types that vehicle's driver cap supports.
func (s *Service) ServeSchema(w http.ResponseWriter, r *http.Request) {
	st := s.ready503(w, r)
	if st == nil {
		return
	}
	if q := r.URL.Query().Get("refresh"); q != "" && q != "0" && q != "false" {
		fresh, err := s.load(r.Context(), nil)
		if err != nil {
			writeError(w, http.StatusServiceUnavailable, "Reloading the schema failed: "+err.Error())
			return
		}
		s.mu.Lock()
		s.state = fresh
		s.scoped = map[string]*State{}
		s.mu.Unlock()
		st = fresh
	}
	if name := r.URL.Query().Get("vehicle"); name != "" {
		plugin, ok := s.vehiclePlugin(w, r, name)
		if !ok {
			return
		}
		var err error
		if st, err = s.scopedState(r.Context(), plugin); err != nil {
			writeError(w, http.StatusServiceUnavailable, fmt.Sprintf("Loading the types of %s failed: %v", name, err))
			return
		}
	}
	writeJSON(w, http.StatusOK, st.Schema)
}

// ServeCompile serves POST /api/compile: a check build for amd64, with the
// driver config of the request's vehicle if it names one.
func (s *Service) ServeCompile(w http.ResponseWriter, r *http.Request) {
	if s.ready503(w, r) == nil {
		return
	}
	var g graphRequest
	if !decodeBody(w, r, &g) {
		return
	}
	target := BuildTarget{Key: "amd64", Arch: "amd64", Engines: map[string]string{}}
	if g.Vehicle != "" {
		var ok bool
		if target.Plugin, ok = s.vehiclePlugin(w, r, g.Vehicle); !ok {
			return
		}
	}
	if !g.Flow.Empty() {
		if s.catalog == nil {
			writeError(w, http.StatusServiceUnavailable, noDaemon)
			return
		}
		cat, err := s.catalog(r.Context())
		if err != nil {
			writeError(w, http.StatusServiceUnavailable, "Cannot list the flow catalog: "+err.Error())
			return
		}
		res, err := s.resolveForValidate(cat, *g.Flow, g.Graph)
		if err != nil {
			writeJSON(w, http.StatusOK, map[string]any{"errors": []GraphError{{Message: err.Error()}}})
			return
		}
		target.Flow, target.Manifest, target.Engines = res.Flow, res.Manifest, res.Engines
	}
	_, errs, err := s.CompileGraph(r.Context(), g.Graph, []BuildTarget{target})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if len(errs) > 0 {
		writeJSON(w, http.StatusOK, map[string]any{"errors": errs})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

// resolveForValidate checks the flow and resolves it for the vehicle it was
// designed for, or, when that is unset or unknown, for a vehicle on the
// orchestrator whose cameras aren't checked.
func (s *Service) resolveForValidate(cat flows.Catalog, t flows.Template, g Graph) (flows.Resolved, error) {
	if err := t.Check(cat, s.gcsIP); err != nil {
		return flows.Resolved{}, err
	}
	target := fleet.Vehicle{DaemonIP: s.gcsIP}
	var cameras []string
	if v, ok := cat.VehicleByName(t.Vehicle); ok && t.Vehicle != "" {
		target.Name = v.Name
		target.DaemonIP, _ = fleet.SplitName(v.Name)
		if v.Typed {
			cameras = []string{}
			for _, st := range v.Streams {
				if st.Kind == "camera" {
					cameras = append(cameras, st.Name)
				}
			}
		}
	}
	return t.Resolve(cat, s.gcsIP, target, cameras, validateMissionName, GraphRoles(g))
}

// ServeRenderDSL serves POST /api/render_dsl.
func (s *Service) ServeRenderDSL(w http.ResponseWriter, r *http.Request) {
	st := s.ready503(w, r)
	if st == nil {
		return
	}
	var g graphRequest
	if !decodeBody(w, r, &g) {
		return
	}
	ast, err := GraphToAst(g.Graph, st.Index, s.header, nil)
	if err == nil {
		var dsl string
		if dsl, err = AstToDSL(ast); err == nil {
			writeJSON(w, http.StatusOK, map[string]any{"dsl": dsl})
			return
		}
	}
	writeError(w, http.StatusUnprocessableEntity, err.Error())
}

// ServeParseDSL serves POST /api/parse_dsl.
func (s *Service) ServeParseDSL(w http.ResponseWriter, r *http.Request) {
	st := s.ready503(w, r)
	if st == nil {
		return
	}
	var req struct {
		DSL string `json:"dsl"`
	}
	if !decodeBody(w, r, &req) {
		return
	}
	dsl, upgrade := UpgradeLegacyDSL(req.DSL)
	ast, err := ParseDSL(dsl)
	if err != nil {
		writeError(w, http.StatusUnprocessableEntity, err.Error())
		return
	}
	graph := AstToGraph(ast, st.Index)
	warnings := []any{}
	for _, m := range upgrade {
		warnings = append(warnings, m)
	}
	if more, ok := graph["warnings"].([]any); ok {
		warnings = append(warnings, more...)
	}
	configured := map[string]bool{}
	for _, i := range s.cfg.Imports {
		configured[i.Path] = true
	}
	var extra []string
	if imports, ok := graph["imports"].([]any); ok {
		for _, i := range imports {
			if m, ok := i.(map[string]any); ok {
				if p, _ := m["path"].(string); !configured[p] {
					extra = append(extra, p)
				}
			}
		}
	}
	if len(extra) > 0 {
		warnings = append(warnings, "Imports not in the GCS configuration are ignored when compiling: "+strings.Join(extra, ", "))
	}
	graph["warnings"] = warnings
	writeJSON(w, http.StatusOK, graph)
}

// ServeBuild serves POST /api/build: the binary for one architecture, with
// the driver config of the request's vehicle if it names one.
func (s *Service) ServeBuild(w http.ResponseWriter, r *http.Request) {
	if s.ready503(w, r) == nil {
		return
	}
	var req graphRequest
	if !decodeBody(w, r, &req) {
		return
	}
	if req.Arch != "amd64" && req.Arch != "arm64" {
		writeError(w, http.StatusUnprocessableEntity, "arch must be amd64 or arm64")
		return
	}
	target := BuildTarget{Key: req.Arch, Arch: req.Arch}
	if req.Vehicle != "" {
		var ok bool
		if target.Plugin, ok = s.vehiclePlugin(w, r, req.Vehicle); !ok {
			return
		}
	}
	bins, errs, err := s.CompileGraph(r.Context(), req.Graph, []BuildTarget{target})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if len(errs) > 0 {
		msgs := make([]string, len(errs))
		for i, e := range errs {
			msgs[i] = e.Message
		}
		writeError(w, http.StatusUnprocessableEntity, fmt.Sprintf("Build failed for %s: %s", req.Arch, strings.Join(msgs, "; ")))
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Content-Disposition", fmt.Sprintf("attachment; filename=%q", "mission-"+req.Arch))
	_, _ = w.Write(bins[req.Arch])
}

// CompileGraph compiles g for every target concurrently and returns the
// binaries by BuildTarget.Key. A non-empty []GraphError means the mission
// was rejected (structural, geojson or compiler errors, deduplicated across
// targets; with several targets an error only some of them reported is
// prefixed with "<Key>: "). The error is non-nil when the compiler itself
// failed (not a rejected mission); it is returned unwrapped and callers add
// their own context.
func (s *Service) CompileGraph(ctx context.Context, g Graph, targets []BuildTarget) (map[string][]byte, []GraphError, error) {
	st, err := s.Ready(ctx)
	if err != nil {
		return nil, nil, err
	}
	errs := StructuralErrors(g)
	geojson, featureErrs := PrepareGeoJSON(g.GeoJSON)
	for _, m := range featureErrs {
		errs = append(errs, GraphError{Message: m})
	}
	if len(errs) > 0 {
		return nil, errs, nil
	}
	// One AST per distinct engine map; targets with the same map share it.
	asts := make([]map[string]any, len(targets))
	byEngines := map[string]map[string]any{}
	for i, t := range targets {
		k := enginesKey(t.Engines)
		ast, ok := byEngines[k]
		if !ok {
			if ast, err = GraphToAst(g, st.Index, s.header, t.Engines); err != nil {
				return nil, []GraphError{{Message: err.Error()}}, nil
			}
			byEngines[k] = ast
		}
		asts[i] = ast
	}

	bins := make([][]byte, len(targets))
	failures := make([]error, len(targets))
	var wg sync.WaitGroup
	for i, t := range targets {
		wg.Add(1)
		go func() {
			defer wg.Done()
			bins[i], failures[i] = s.c.Build(ctx, BuildRequest{
				Ast: asts[i], GeoJSON: geojson, Plugin: t.Plugin, Arch: t.Arch, Flow: t.Flow, Manifest: t.Manifest,
			})
		}()
	}
	wg.Wait()

	perTarget := make([][]GraphError, len(targets))
	for i, failure := range failures {
		if failure == nil {
			continue
		}
		var ce *CompileErrors
		if !errors.As(failure, &ce) {
			return nil, nil, failure
		}
		perTarget[i] = MapErrors(ce.Errors, g)
	}
	if merged := mergeErrors(targets, perTarget); len(merged) > 0 {
		return nil, merged, nil
	}
	out := make(map[string][]byte, len(targets))
	for i, t := range targets {
		out[t.Key] = bins[i]
	}
	return out, nil, nil
}

// enginesKey identifies an engine map; nil and empty maps differ.
func enginesKey(m map[string]string) string {
	b, _ := json.Marshal(m) // sorted keys; nil is "null"
	return string(b)
}

func errKey(e GraphError) string {
	b, _ := json.Marshal(e)
	return string(b)
}

// mergeErrors combines per-target compile errors: one reported by every
// target appears once, unprefixed; others get their target's key prefixed.
// A single target is never prefixed.
func mergeErrors(targets []BuildTarget, perTarget [][]GraphError) []GraphError {
	has := make([]map[string]bool, len(perTarget))
	for i, errs := range perTarget {
		has[i] = map[string]bool{}
		for _, e := range errs {
			has[i][errKey(e)] = true
		}
	}
	var out []GraphError
	seen := map[string]bool{}
	for i, errs := range perTarget {
		for _, e := range errs {
			k := errKey(e)
			if len(targets) > 1 {
				common := true
				for j := range perTarget {
					if !has[j][k] {
						common = false
						break
					}
				}
				if !common {
					e.Message = targets[i].Key + ": " + e.Message
					k = errKey(e)
				}
			}
			if !seen[k] {
				seen[k] = true
				out = append(out, e)
			}
		}
	}
	return out
}

func writeError(w http.ResponseWriter, code int, detail string) {
	writeJSON(w, code, map[string]string{"detail": detail})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
