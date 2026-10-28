package mission

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"runtime/debug"
	"strings"
	"time"

	"github.com/cmusatyalab/steeleagle/sdk/buildtools"
	"github.com/cmusatyalab/steeleagle/sdk/dsl/compiler"
)

const (
	sdkModulePath = "github.com/cmusatyalab/steeleagle/sdk"
	defaultTimeS  = 300
)

// defaultImports are the packages every mission imports; configured imports
// are added after these.
var defaultImports = []string{"dsl/actions", "dsl/events", "dsl/datatypes", "params", "enums"}

// Module is a Go module missions are compiled against.
type Module struct {
	Path     string `json:"path"`
	Version  string `json:"version"`
	Override string `json:"override"`
}

// Import is a package imported by every mission.
type Import struct {
	Path  string `json:"path"`
	Alias string `json:"alias"`
}

// CompilerConfig is the [compiler] section of the GCS config.
type CompilerConfig struct {
	Modules    []Module `json:"modules"`
	Imports    []Import `json:"imports"`
	TimeoutS   float64  `json:"timeout_s"`
	SDKVersion string   `json:"sdk_version"`
}

// allImports returns the default imports followed by the configured ones,
// without duplicate paths.
func (c CompilerConfig) allImports() []Import {
	var out []Import
	seen := map[string]bool{}
	add := func(i Import) {
		if !seen[i.Path] {
			seen[i.Path] = true
			out = append(out, i)
		}
	}
	for _, p := range defaultImports {
		add(Import{Path: sdkModulePath + "/" + p})
	}
	for _, i := range c.Imports {
		add(i)
	}
	return out
}

// Header returns the Modules: and Imports: stanzas every mission is
// compiled with, as AST JSON. Empty fields are left out.
func (c CompilerConfig) Header() map[string]any {
	modules := []any{}
	for _, m := range c.Modules {
		e := map[string]any{"Path": m.Path}
		if m.Version != "" {
			e["Version"] = m.Version
		}
		if m.Override != "" {
			e["Override"] = m.Override
		}
		modules = append(modules, e)
	}
	imports := []any{}
	for _, i := range c.allImports() {
		e := map[string]any{"Path": i.Path}
		if i.Alias != "" {
			e["Alias"] = i.Alias
		}
		imports = append(imports, e)
	}
	return map[string]any{
		"Modules": map[string]any{"Modules": modules},
		"Imports": map[string]any{"Imports": imports},
	}
}

// buildInfoSDKVersion returns the SDK version this binary was built with.
func buildInfoSDKVersion() string {
	bi, ok := debug.ReadBuildInfo()
	if !ok {
		return ""
	}
	for _, d := range bi.Deps {
		if d.Path == sdkModulePath {
			if d.Replace != nil && d.Replace.Version != "" {
				return d.Replace.Version
			}
			return d.Version
		}
	}
	return ""
}

// ResolveConfig applies the defaults: a 300 s timeout, the default imports
// and an SDK pin (sdk_version, an SDK modules entry, or else the SDK version
// this binary was built with). It fails if no SDK version can be determined.
func ResolveConfig(c CompilerConfig) (CompilerConfig, error) {
	out := CompilerConfig{TimeoutS: c.TimeoutS, SDKVersion: c.SDKVersion}
	if out.TimeoutS <= 0 {
		out.TimeoutS = defaultTimeS
	}
	out.Imports = c.allImports()
	var configured *Module
	for i, m := range c.Modules {
		if m.Path == sdkModulePath {
			configured = &c.Modules[i]
			break
		}
	}
	switch {
	case configured != nil:
		out.Modules = append(out.Modules, *configured)
	case c.SDKVersion != "":
		out.Modules = append(out.Modules, Module{Path: sdkModulePath, Version: c.SDKVersion})
	default:
		v := buildInfoSDKVersion()
		if v == "" || v == "(devel)" {
			return CompilerConfig{}, errors.New("cannot determine the SDK version: set compiler.sdk_version or a modules override for the SDK")
		}
		out.Modules = append(out.Modules, Module{Path: sdkModulePath, Version: v})
	}
	seen := map[string]bool{sdkModulePath: true}
	for _, m := range c.Modules {
		if !seen[m.Path] {
			seen[m.Path] = true
			out.Modules = append(out.Modules, m)
		}
	}
	return out, nil
}

// BuildRequest is a mission to compile.
type BuildRequest struct {
	Ast     map[string]any
	GeoJSON []byte // nil if the mission has no areas
	Plugin  []byte // driver plugin config TOML, nil if none
	Arch    string // GOARCH of the vehicle
	// Flow and Manifest are the resolved flow config and mission manifest
	// TOML, nil if the mission has no flow.
	Flow     []byte
	Manifest []byte
}

// CompileErrors is the error returned when the compiler rejected the mission.
// Any other error means the compiler itself failed.
type CompileErrors struct{ Errors []DeclError }

// Error implements error.
func (e *CompileErrors) Error() string {
	msgs := make([]string, len(e.Errors))
	for i, d := range e.Errors {
		msgs[i] = d.Message
	}
	return strings.Join(msgs, "; ")
}

// Compiler compiles missions and reports the types available to them.
type Compiler interface {
	// Registry returns the type registry missions are linked against,
	// scrubbed by the driver cap of plugin (a driver plugin config TOML), or
	// unscrubbed if plugin is nil.
	Registry(ctx context.Context, plugin []byte) (map[string]any, error)
	// Build compiles a mission into a binary.
	Build(ctx context.Context, req BuildRequest) ([]byte, error)
}

// sdkCompiler compiles with the SDK in-process. The SDK takes no context, so
// a compile that times out or is cancelled is abandoned, not stopped: it
// runs to completion, its result is discarded, and it keeps holding its
// slot of the concurrency limit until then.
type sdkCompiler struct {
	cfg      CompilerConfig
	sem      chan struct{}
	compile  func(req BuildRequest) ([]byte, error)
	registry func(plugin []byte) ([]byte, error)
}

// NewSDKCompiler returns a Compiler using the SDK, with at most
// maxConcurrent compiles running at once. cfg should be resolved.
func NewSDKCompiler(c CompilerConfig, maxConcurrent int) Compiler {
	if maxConcurrent < 1 {
		maxConcurrent = 1
	}
	s := &sdkCompiler{cfg: c, sem: make(chan struct{}, maxConcurrent)}
	s.compile = s.sdkBuild
	s.registry = s.sdkRegistry
	return s
}

func (s *sdkCompiler) Registry(ctx context.Context, plugin []byte) (map[string]any, error) {
	raw, err := s.run(ctx, func() ([]byte, error) { return s.registry(plugin) })
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decoding type registry: %w", err)
	}
	return out, nil
}

func (s *sdkCompiler) Build(ctx context.Context, req BuildRequest) ([]byte, error) {
	return s.run(ctx, func() ([]byte, error) { return s.compile(req) })
}

type runResult struct {
	out []byte
	err error
}

// run runs fn once a concurrency slot is free, waiting at most the
// configured timeout (slot wait included) and until ctx is done.
func (s *sdkCompiler) run(ctx context.Context, fn func() ([]byte, error)) ([]byte, error) {
	timeoutS := s.cfg.TimeoutS
	if timeoutS <= 0 {
		timeoutS = defaultTimeS
	}
	timer := time.NewTimer(time.Duration(timeoutS * float64(time.Second)))
	defer timer.Stop()
	timedOut := fmt.Errorf("timed out after %g s", timeoutS)

	select {
	case s.sem <- struct{}{}:
	case <-timer.C:
		return nil, timedOut
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	done := make(chan runResult, 1)
	go func() {
		defer func() { <-s.sem }()
		defer func() {
			if r := recover(); r != nil {
				done <- runResult{err: fmt.Errorf("compiler panicked: %v", r)}
			}
		}()
		out, err := fn()
		done <- runResult{out, err}
	}()
	select {
	case r := <-done:
		return r.out, r.err
	case <-timer.C:
		return nil, timedOut
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func (s *sdkCompiler) sdkRegistry(plugin []byte) ([]byte, error) {
	hdr, err := json.Marshal(s.cfg.Header())
	if err != nil {
		return nil, err
	}
	w, err := compiler.NewWorkspace(nil)
	if err != nil {
		return nil, err
	}
	defer w.Close()
	var opts []buildtools.Option
	if plugin != nil {
		opts = append(opts, buildtools.WithPlugin(plugin))
	}
	out, errs := w.Load(compiler.FromJson(hdr), opts...)
	if err := convertErrors(errs); err != nil {
		return nil, err
	}
	return out, nil
}

func (s *sdkCompiler) sdkBuild(req BuildRequest) ([]byte, error) {
	ast, err := json.Marshal(req.Ast)
	if err != nil {
		return nil, err
	}
	// The output goes outside the workspace, which Close removes.
	f, err := os.CreateTemp("", "mission-*.bin")
	if err != nil {
		return nil, err
	}
	f.Close()
	defer os.Remove(f.Name())
	opts := buildOptions(req, f.Name())
	w, err := compiler.NewWorkspace([]string{"GOARCH=" + req.Arch})
	if err != nil {
		return nil, err
	}
	defer w.Close()
	if err := convertErrors(w.Compile(compiler.FromJson(ast), opts...)); err != nil {
		return nil, err
	}
	return os.ReadFile(f.Name())
}

// buildOptions returns the SDK build options for req, writing to output.
// Inputs that are nil are left out.
func buildOptions(req BuildRequest, output string) []buildtools.Option {
	opts := []buildtools.Option{buildtools.WithOutputFile(output)}
	if req.GeoJSON != nil {
		opts = append(opts, buildtools.WithGeoJson(req.GeoJSON))
	}
	if req.Plugin != nil {
		opts = append(opts, buildtools.WithPlugin(req.Plugin))
	}
	if req.Flow != nil {
		opts = append(opts, buildtools.WithFlow(req.Flow))
	}
	if req.Manifest != nil {
		opts = append(opts, buildtools.WithManifest(req.Manifest))
	}
	return opts
}

// convertErrors maps SDK compile errors to *CompileErrors when any is about
// the mission (has a decl or file), and to a plain error otherwise.
func convertErrors(errs []*buildtools.CompileError) error {
	if len(errs) == 0 {
		return nil
	}
	mission := false
	var out []DeclError
	var msgs []string
	for _, e := range errs {
		if e.Decl != "" || e.File != "" {
			mission = true
		}
		out = append(out, DeclError{Decl: e.Decl, Message: e.Err.Error()})
		msgs = append(msgs, e.Err.Error())
	}
	if mission {
		return &CompileErrors{Errors: out}
	}
	return fmt.Errorf("%v", strings.Join(msgs, "; "))
}
