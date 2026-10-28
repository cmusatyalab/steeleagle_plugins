package flows

import (
	"fmt"
	"strings"

	"github.com/BurntSushi/toml"
	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
)

// genericResult is the result declared for an engine whose result type isn't
// one the SDK generates a typed engine field for.
const genericResult = "messages/result/GenericResult"

// telemetryProducer is the producer every vehicle has, which needs no camera.
const telemetryProducer = "telemetry"

// TemplateProducer is a producer of a flow template. Name is {vehicle}.{stream},
// where the vehicle is the one the flow was designed on, only as a label: each
// target uses its own stream of that name.
type TemplateProducer struct {
	Name   string `json:"name"`
	Tokens *int   `json:"tokens,omitempty"`
}

// TemplateEngine is an engine of a flow template. Name is {host}.{engine}.
type TemplateEngine struct {
	Name string `json:"name"`
}

// TemplateEdge is an edge of a flow template. Its endpoints are template
// producer or engine names.
type TemplateEdge struct {
	From    string `json:"from"`
	To      string `json:"to"`
	Timeout string `json:"timeout"`
}

// Template is a flow as the Flow Builder designs it, before it is resolved
// for a vehicle. Vehicle is the full name of the vehicle it was designed for.
type Template struct {
	Vehicle   string             `json:"vehicle"`
	Producers []TemplateProducer `json:"producers"`
	Engines   []TemplateEngine   `json:"engines"`
	Edges     []TemplateEdge     `json:"edges"`
}

// Resolved is a template resolved for one vehicle. Flow and Manifest are TOML.
// Engines maps each template engine name to its resolved full name, {ip}.{name}.
type Resolved struct {
	Flow     []byte
	Manifest []byte
	Engines  map[string]string
}

// Empty reports whether the template is nil or has no producers and no engines.
func (t *Template) Empty() bool {
	return t == nil || (len(t.Producers) == 0 && len(t.Engines) == 0)
}

// ParseTemplateTOML parses a template from the TOML of a flow config. The
// result has no Vehicle.
func ParseTemplateTOML(data []byte) (Template, error) {
	var f config.FlowConfig
	meta, err := toml.Decode(string(data), &f)
	if err != nil {
		return Template{}, err
	}
	// An old-format or misspelled document would otherwise parse as empty
	if undecoded := meta.Undecoded(); len(undecoded) > 0 {
		keys := make([]string, len(undecoded))
		for i, k := range undecoded {
			keys[i] = k.String()
		}
		return Template{}, fmt.Errorf("unknown flow keys %s", strings.Join(keys, ", "))
	}
	t := Template{}
	for _, p := range f.Producers {
		t.Producers = append(t.Producers, TemplateProducer{Name: p.Name, Tokens: p.Tokens})
	}
	for _, e := range f.Engines {
		// The location is per target, so a template doesn't keep it.
		t.Engines = append(t.Engines, TemplateEngine{Name: e.Name})
	}
	for _, e := range f.Edges {
		t.Edges = append(t.Edges, TemplateEdge{From: e.From, To: e.To, Timeout: e.Timeout})
	}
	return t, nil
}

// hostName is a template name split into the IP of the daemon its host names
// and its bare name.
type hostName struct{ IP, Bare string }

// resolveNames splits every producer into its stream, and resolves the host of
// every engine, returning the results in template order. A producer's vehicle
// prefix is only a label, so it isn't resolved. Each engine must exist in the
// catalog, and no two may be the same engine.
func (t Template) resolveNames(c Catalog) (producers, engines []hostName, err error) {
	resolve := func(name string) (hostName, error) {
		host, bare, ok := SplitTemplateName(name)
		if !ok {
			return hostName{}, fmt.Errorf("flow name %s has no host", name)
		}
		ip, err := c.ResolveHost(host)
		if err != nil {
			return hostName{}, err
		}
		return hostName{ip, bare}, nil
	}
	for _, p := range t.Producers {
		_, stream, ok := SplitTemplateName(p.Name)
		if !ok {
			stream = p.Name
		}
		producers = append(producers, hostName{Bare: stream})
	}
	seen := make(map[string]string, len(t.Engines))
	for _, e := range t.Engines {
		h, err := resolve(e.Name)
		if err != nil {
			return nil, nil, err
		}
		full := h.IP + "." + h.Bare
		if _, ok := c.Engine(full); !ok {
			host, _, _ := SplitTemplateName(e.Name)
			return nil, nil, fmt.Errorf("flow engine %s is not on %s", e.Name, host)
		}
		if first, dup := seen[full]; dup {
			return nil, nil, fmt.Errorf("flow engines %s and %s are the same engine", first, e.Name)
		}
		seen[full] = e.Name
		engines = append(engines, h)
	}
	return producers, engines, nil
}

// EngineNames maps each engine's template name to its full name, {ip}.{name},
// failing as Check does if a name doesn't resolve.
func (t Template) EngineNames(c Catalog) (map[string]string, error) {
	_, engines, err := t.resolveNames(c)
	if err != nil {
		return nil, err
	}
	return engineMap(t, engines), nil
}

func engineMap(t Template, engines []hostName) map[string]string {
	m := make(map[string]string, len(engines))
	for i, e := range t.Engines {
		m[e.Name] = engines[i].IP + "." + engines[i].Bare
	}
	return m
}

// flowConfig builds the flow config of the template for a daemon: producers
// take their bare stream names, engines their full names, and an engine is
// local if it is on daemonIP and remote otherwise.
func (t Template) flowConfig(producers, engines []hostName, daemonIP string) *config.FlowConfig {
	rename := make(map[string]string, len(producers)+len(engines))
	f := &config.FlowConfig{}
	for i, p := range t.Producers {
		rename[p.Name] = producers[i].Bare
		f.Producers = append(f.Producers, config.Producer{Name: producers[i].Bare, Tokens: p.Tokens})
	}
	for i, e := range t.Engines {
		full := engines[i].IP + "." + engines[i].Bare
		rename[e.Name] = full
		loc := config.LocationRemote
		if engines[i].IP == daemonIP {
			loc = config.LocationLocal
		}
		f.Engines = append(f.Engines, config.Engine{Name: full, Location: loc})
	}
	// An endpoint that names no node is left for Validate to reject.
	name := func(n string) string {
		if r, ok := rename[n]; ok {
			return r
		}
		return n
	}
	for _, e := range t.Edges {
		f.Edges = append(f.Edges, config.Edge{From: name(e.From), To: name(e.To), Timeout: e.Timeout})
	}
	return f
}

// Check runs the checks that don't depend on the target: every engine resolves
// to a daemon that runs it, and the flow passes
// config.FlowConfig.Validate. Engines are located relative to the daemon of
// the vehicle the flow was designed for, or all remote if that vehicle is
// unset or not in the catalog; only the flow's structure is being checked, so
// the orchestrator's IP isn't needed.
func (t Template) Check(c Catalog, _ string) error {
	producers, engines, err := t.resolveNames(c)
	if err != nil {
		return err
	}
	vehicleIP := ""
	if v, ok := c.VehicleByName(t.Vehicle); ok {
		vehicleIP, _ = fleet.SplitName(v.Name)
	}
	return t.flowConfig(producers, engines, vehicleIP).Validate()
}

// Resolve resolves the template for target. The target uses its own stream of
// each producer's name, so every producer other than telemetry must be one of
// cameras. If cameras is nil the camera check is skipped (Validate resolves for
// a synthetic target whose cameras are unknown); a non-nil slice, even an empty
// one, is checked. An engine on the target's daemon is local, one on the
// orchestrator's daemon (orchestratorIP) is remote, and any other fails.
// missionName and roles go to the manifest.
func (t Template) Resolve(c Catalog, orchestratorIP string, target fleet.Vehicle, cameras []string, missionName string, roles []string) (Resolved, error) {
	producers, engines, err := t.resolveNames(c)
	if err != nil {
		return Resolved{}, err
	}
	have := make(map[string]bool, len(cameras))
	for _, n := range cameras {
		have[n] = true
	}
	for i := range t.Producers {
		if cameras != nil && producers[i].Bare != telemetryProducer && !have[producers[i].Bare] {
			return Resolved{}, fmt.Errorf("flow needs camera %s, which this vehicle lacks", producers[i].Bare)
		}
	}
	manifest := config.MissionManifest{
		Info:    config.InfoField{Name: missionName, Version: "", Roles: roles, Squawks: []string{}},
		Engines: []config.MissionEngineField{},
	}
	for i, e := range t.Engines {
		if ip := engines[i].IP; ip != target.DaemonIP && ip != orchestratorIP {
			return Resolved{}, fmt.Errorf("flow engine %s is neither on this vehicle's daemon nor on the orchestrator", e.Name)
		}
		full := engines[i].IP + "." + engines[i].Bare
		ce, _ := c.Engine(full) // resolveNames found it
		manifest.Engines = append(manifest.Engines, config.MissionEngineField{Name: full, Result: ManifestResult(ce.Result)})
	}
	fc := t.flowConfig(producers, engines, target.DaemonIP)
	if err := fc.Validate(); err != nil {
		return Resolved{}, err
	}
	flow, err := fc.ToBytes()
	if err != nil {
		return Resolved{}, err
	}
	mb, err := manifest.ToBytes()
	if err != nil {
		return Resolved{}, err
	}
	return Resolved{Flow: flow, Manifest: mb, Engines: engineMap(t, engines)}, nil
}

// ManifestResult returns the result type to declare in a manifest for an
// engine whose result is result: the result itself if its last path segment is
// one of the five result types the SDK generates engine fields for, otherwise
// the Generic stand-in.
func ManifestResult(result string) string {
	seg := result[strings.LastIndex(result, "/")+1:]
	switch seg {
	case "DetectionResult", "ClassificationResult", "GuidanceResult", "PositionResult", "GenericResult":
		return result
	}
	return genericResult
}
