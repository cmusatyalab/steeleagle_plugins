package flows

import (
	"fmt"
	"strings"

	"github.com/BurntSushi/toml"
	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
)

// telemetryProducer is the producer every vehicle has, which needs no camera.
const telemetryProducer = "telemetry"

// TemplateProducer is a producer of a flow template. Name is either
// {vehicle}.{stream}, where the vehicle is the one the flow was designed on,
// only as a label: each target uses its own stream of that name; or
// {host}.{engine}.{producer}, a producer the engine advertises.
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
// Engines maps each template engine name to its name in the vehicle's flow
// and mission, local.<engine> or remote.<engine>.
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
// and its bare name. A vehicle stream has only a Bare stream name; an engine
// producer has its engine's IP and Bare name, and its Producer name.
type hostName struct{ IP, Bare, Producer string }

// flowName is the node's name in a flow config: a vehicle stream's bare name
// (the vehicle qualifies it), an engine's full name, or an engine producer's
// {ip}.{engine}.{producer}, the name its engine advertises it under.
func (h hostName) flowName() string {
	switch {
	case h.IP == "":
		return h.Bare
	case h.Producer == "":
		return h.IP + "." + h.Bare
	default:
		return h.IP + "." + h.Bare + "." + h.Producer
	}
}

// engineProducer resolves name as {host}.{engine}.{producer}. ok is false if
// name isn't of that form or doesn't name a catalog engine, so it is a
// vehicle stream; err is set if it names a catalog engine that doesn't
// advertise the producer.
func (c Catalog) engineProducer(name string) (h hostName, ok bool, err error) {
	engine, producer, found := SplitTemplateName(name)
	if !found {
		return hostName{}, false, nil
	}
	host, bare, found := SplitTemplateName(engine)
	if !found {
		return hostName{}, false, nil
	}
	ip, resolveErr := c.ResolveHost(host)
	if resolveErr != nil {
		return hostName{}, false, nil
	}
	e, found := c.Engine(ip + "." + bare)
	if !found {
		return hostName{}, false, nil
	}
	if _, found := e.Producer(producer); !found {
		return hostName{}, true, fmt.Errorf("flow producer %s: engine %s has no producer %s", name, engine, producer)
	}
	return hostName{IP: ip, Bare: bare, Producer: producer}, true, nil
}

// resolveNames splits every producer into its stream, or resolves it to the
// engine producer it names, and resolves the host of every engine, returning
// the results in template order. A vehicle stream's vehicle prefix is only a
// label, so it isn't resolved. Each engine must exist in the catalog, no two
// may be the same engine, and no two producers the same producer.
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
		return hostName{IP: ip, Bare: bare}, nil
	}
	seenProducer := make(map[string]string, len(t.Producers))
	for _, p := range t.Producers {
		h, ok, err := c.engineProducer(p.Name)
		if err != nil {
			return nil, nil, err
		}
		if !ok {
			_, stream, found := SplitTemplateName(p.Name)
			if !found {
				stream = p.Name
			}
			h = hostName{Bare: stream}
		}
		if first, dup := seenProducer[h.flowName()]; dup {
			return nil, nil, fmt.Errorf("flow producers %s and %s are the same producer", first, p.Name)
		}
		seenProducer[h.flowName()] = p.Name
		producers = append(producers, h)
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

// EngineNames maps each engine's template name to its name in a flow and a
// mission, local.<engine> or remote.<engine>, relative to the daemon of the
// vehicle the flow was designed for (all remote if that is unknown), failing
// as Check does if a name doesn't resolve. Each target's own names come with
// its Resolve.
func (t Template) EngineNames(c Catalog) (map[string]string, error) {
	_, engines, err := t.resolveNames(c)
	if err != nil {
		return nil, err
	}
	return engineMap(t, engines, t.vehicleIP(c)), nil
}

// vehicleIP is the daemon IP of the vehicle the flow was designed for, or ""
// if it is unset or not in the catalog.
func (t Template) vehicleIP(c Catalog) string {
	if v, ok := c.VehicleByName(t.Vehicle); ok {
		ip, _ := fleet.SplitName(v.Name)
		return ip
	}
	return ""
}

func engineMap(t Template, engines []hostName, daemonIP string) map[string]string {
	m := make(map[string]string, len(engines))
	for i, e := range t.Engines {
		m[e.Name], _ = fleet.FlowEngine(engines[i].IP+"."+engines[i].Bare, daemonIP)
	}
	return m
}

// flowConfig builds the flow config of the template for a vehicle on the
// daemon daemonIP, with the names the vehicle qualifies itself (see
// fleet/flownames.go): vehicle.<stream>, local.<engine>.<producer>, and
// local.<engine> or remote.<engine>, local if it is on daemonIP and remote
// otherwise. An engine producer must be of an engine on daemonIP, since the
// vehicle only takes its local engines' producers; with daemonIP unknown ("")
// that check is left to each target.
func (t Template) flowConfig(producers, engines []hostName, daemonIP string) (*config.FlowConfig, error) {
	rename := make(map[string]string, len(producers)+len(engines))
	f := &config.FlowConfig{}
	for i, p := range t.Producers {
		h := producers[i]
		name := fleet.VehicleStream(h.Bare)
		if h.Producer != "" {
			if daemonIP != "" && h.IP != daemonIP {
				return nil, fmt.Errorf("flow producer %s is of a remote engine; only producers of engines on the vehicle's daemon can be used", p.Name)
			}
			name = fleet.LocalEngineProducer(h.Bare, h.Producer)
		}
		rename[p.Name] = name
		f.Producers = append(f.Producers, config.Producer{Name: name, Tokens: p.Tokens})
	}
	for i, e := range t.Engines {
		name, loc := fleet.FlowEngine(engines[i].IP+"."+engines[i].Bare, daemonIP)
		rename[e.Name] = name
		f.Engines = append(f.Engines, config.Engine{Name: name, Location: loc})
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
	return f, nil
}

// Check runs the checks that don't depend on the target: every engine resolves
// to a daemon that runs it, and the flow passes
// config.FlowConfig.Validate. Engines are located relative to the daemon of
// the vehicle the flow was designed for, or all remote if that vehicle is
// unset or not in the catalog; only the flow's structure is being checked, so
// the orchestrator's IP isn't needed. With the vehicle known, its engine
// producers must be of engines on its daemon.
func (t Template) Check(c Catalog, _ string) error {
	producers, engines, err := t.resolveNames(c)
	if err != nil {
		return err
	}
	fc, err := t.flowConfig(producers, engines, t.vehicleIP(c))
	if err != nil {
		return err
	}
	return fc.Validate()
}

// Resolve resolves the template for target. The target uses its own stream of
// each vehicle producer's name, so every one other than telemetry must be one
// of cameras. If cameras is nil the camera check is skipped (Validate
// resolves for a synthetic target whose cameras are unknown); a non-nil
// slice, even an empty one, is checked. An engine producer must be of an
// engine on the target's daemon. An engine on the target's daemon is local,
// one on the orchestrator's daemon (orchestratorIP) is remote, and any other
// fails. The flow, manifest and Engines name engines relative to the target,
// as local.<engine> or remote.<engine>. missionName and roles go to the
// manifest.
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
		if producers[i].Producer == "" && cameras != nil && producers[i].Bare != telemetryProducer && !have[producers[i].Bare] {
			return Resolved{}, fmt.Errorf("flow needs camera %s, which this vehicle lacks", producers[i].Bare)
		}
	}
	manifest := config.MissionManifest{
		Info:    config.InfoField{Name: missionName, Version: "", Roles: roles, Squawks: []string{}},
		Engines: []config.MissionEngineField{},
	}
	names := engineMap(t, engines, target.DaemonIP)
	for i, e := range t.Engines {
		if ip := engines[i].IP; ip != target.DaemonIP && ip != orchestratorIP {
			return Resolved{}, fmt.Errorf("flow engine %s is neither on this vehicle's daemon nor on the orchestrator", e.Name)
		}
		ce, _ := c.Engine(engines[i].IP + "." + engines[i].Bare) // resolveNames found it
		manifest.Engines = append(manifest.Engines, config.MissionEngineField{Name: names[e.Name], Result: ce.Result})
	}
	fc, err := t.flowConfig(producers, engines, target.DaemonIP)
	if err != nil {
		return Resolved{}, err
	}
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
	return Resolved{Flow: flow, Manifest: mb, Engines: names}, nil
}

// ForVehicle is the template with every name relative to a vehicle
// (vehicle.<stream>, local.<engine>[.<producer>], remote.<engine>)
// translated for the vehicle (full name) on the orchestrator at
// orchestratorIP, as Catalog.ForVehicle translates it; names given by host
// are kept. Its Vehicle is the vehicle.
func (t Template) ForVehicle(c Catalog, vehicle, orchestratorIP string) (Template, error) {
	out := Template{Vehicle: vehicle}
	name := func(n string) (string, error) { return c.ForVehicle(n, vehicle, orchestratorIP) }
	for _, p := range t.Producers {
		n, err := name(p.Name)
		if err != nil {
			return Template{}, err
		}
		out.Producers = append(out.Producers, TemplateProducer{Name: n, Tokens: p.Tokens})
	}
	for _, e := range t.Engines {
		n, err := name(e.Name)
		if err != nil {
			return Template{}, err
		}
		out.Engines = append(out.Engines, TemplateEngine{Name: n})
	}
	for _, e := range t.Edges {
		from, err := name(e.From)
		if err != nil {
			return Template{}, err
		}
		to, err := name(e.To)
		if err != nil {
			return Template{}, err
		}
		out.Edges = append(out.Edges, TemplateEdge{From: from, To: to, Timeout: e.Timeout})
	}
	return out, nil
}
