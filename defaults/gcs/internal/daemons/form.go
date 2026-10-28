package daemons

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/BurntSushi/toml"
)

// Form is a daemon config as the Configure dialog edits it. The keys it
// doesn't know are kept in the Extra maps, so a config survives a trip
// through the form unchanged in content (comments and formatting aren't
// kept).
type Form struct {
	Daemon DaemonForm `json:"daemon"`
	// Simulator is nil when the config has no [simulator].
	Simulator *SimulatorForm `json:"simulator"`
	Services  []ServiceForm  `json:"services"`
	// Extra holds the top-level keys other than daemon, simulator and
	// services.
	Extra map[string]any `json:"extra"`
}

// DaemonForm is the [daemon] table. A nil field is a key the config
// doesn't set.
type DaemonForm struct {
	Port         *int64         `json:"port"`
	Lcs          *int64         `json:"lcs"`
	Hostname     *string        `json:"hostname"`
	Cert         *string        `json:"cert"`
	Orchestrator *string        `json:"orchestrator"`
	Extra        map[string]any `json:"extra"`
}

// SimulatorForm is the [simulator] table. Nil Args is no args key.
type SimulatorForm struct {
	Package string         `json:"package"`
	Args    map[string]any `json:"args"`
	Extra   map[string]any `json:"extra"`
}

// ServiceForm is one [[services]] entry. Empty Auth and false Simulate are
// keys the config doesn't set.
type ServiceForm struct {
	Name     string         `json:"name"`
	Type     string         `json:"type"`
	Auth     string         `json:"auth"`
	Simulate bool           `json:"simulate"`
	Plugin   PluginForm     `json:"plugin"`
	Extra    map[string]any `json:"extra"`
}

// PluginForm is a service's [services.plugin] table. Empty strings are keys
// the config doesn't set; nil Args is no args key.
type PluginForm struct {
	Plugin    string         `json:"plugin"`
	Package   string         `json:"package"`
	Isolation string         `json:"isolation"`
	Args      map[string]any `json:"args"`
	Extra     map[string]any `json:"extra"`
}

// TOMLError is a TOML syntax error, in the {message, line, column} shape the
// web UI shows.
type TOMLError struct {
	Message string `json:"message"`
	Line    int    `json:"line"`
	Column  int    `json:"column"`
}

func (e *TOMLError) Error() string {
	return fmt.Sprintf("line %d, column %d: %s", e.Line, e.Column, e.Message)
}

// ErrUnrepresentable is a config that is valid TOML but can't be shown as a
// form; it is edited as raw TOML instead.
var ErrUnrepresentable = errors.New("config can't be shown as a form")

func unrepresentable(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrUnrepresentable, fmt.Sprintf(format, args...))
}

// ParseForm reads a daemon config. A syntax error is a *TOMLError; a config
// the form can't hold wraps ErrUnrepresentable.
func ParseForm(raw []byte) (Form, error) {
	var doc map[string]any
	if _, err := toml.Decode(string(raw), &doc); err != nil {
		var pe toml.ParseError
		if errors.As(err, &pe) {
			return Form{}, &TOMLError{Message: pe.Message, Line: pe.Position.Line, Column: pe.Position.Col}
		}
		return Form{}, &TOMLError{Message: err.Error()}
	}
	if path := findTime(doc, ""); path != "" {
		return Form{}, unrepresentable("%s is a TOML date or time", path)
	}
	var f Form
	var err error
	rest := copyMap(doc)
	if v, ok := take(rest, "daemon"); ok {
		t, ok := v.(map[string]any)
		if !ok {
			return Form{}, unrepresentable("daemon is not a table")
		}
		if f.Daemon, err = parseDaemon(t); err != nil {
			return Form{}, err
		}
	}
	if v, ok := take(rest, "simulator"); ok {
		t, ok := v.(map[string]any)
		if !ok {
			return Form{}, unrepresentable("simulator is not a table")
		}
		sim, err := parseSimulator(t)
		if err != nil {
			return Form{}, err
		}
		f.Simulator = &sim
	}
	if v, ok := take(rest, "services"); ok {
		list, ok := v.([]map[string]any)
		if !ok {
			return Form{}, unrepresentable("services is not an array of tables")
		}
		for i, t := range list {
			s, err := parseService(t)
			if err != nil {
				return Form{}, unrepresentable("services[%d]: %s", i, strings.TrimPrefix(err.Error(), ErrUnrepresentable.Error()+": "))
			}
			f.Services = append(f.Services, s)
		}
	}
	f.Extra = nilIfEmpty(rest)
	return f, nil
}

func parseDaemon(t map[string]any) (DaemonForm, error) {
	var d DaemonForm
	rest := copyMap(t)
	var err error
	if d.Port, err = takeInt(rest, "daemon.port"); err != nil {
		return d, err
	}
	if d.Lcs, err = takeInt(rest, "daemon.lcs"); err != nil {
		return d, err
	}
	for key, dst := range map[string]**string{"hostname": &d.Hostname, "cert": &d.Cert, "orchestrator": &d.Orchestrator} {
		if *dst, err = takeString(rest, "daemon."+key); err != nil {
			return d, err
		}
	}
	d.Extra = nilIfEmpty(rest)
	return d, nil
}

func parseSimulator(t map[string]any) (SimulatorForm, error) {
	var s SimulatorForm
	rest := copyMap(t)
	pkg, err := takeString(rest, "simulator.package")
	if err != nil {
		return s, err
	}
	s.Package = deref(pkg)
	if s.Args, err = takeTable(rest, "simulator.args"); err != nil {
		return s, err
	}
	s.Extra = nilIfEmpty(rest)
	return s, nil
}

func parseService(t map[string]any) (ServiceForm, error) {
	var s ServiceForm
	rest := copyMap(t)
	for key, dst := range map[string]*string{"name": &s.Name, "type": &s.Type, "auth": &s.Auth} {
		v, err := takeString(rest, key)
		if err != nil {
			return s, err
		}
		*dst = deref(v)
	}
	if v, ok := take(rest, "simulate"); ok {
		b, ok := v.(bool)
		if !ok {
			return s, unrepresentable("simulate is not a boolean")
		}
		s.Simulate = b
	}
	plugin, err := takeTable(rest, "plugin")
	if err != nil {
		return s, err
	}
	prest := copyMap(plugin)
	for key, dst := range map[string]*string{"plugin": &s.Plugin.Plugin, "package": &s.Plugin.Package, "isolation": &s.Plugin.Isolation} {
		v, err := takeString(prest, "plugin."+key)
		if err != nil {
			return s, err
		}
		*dst = deref(v)
	}
	if s.Plugin.Args, err = takeTable(prest, "plugin.args"); err != nil {
		return s, err
	}
	s.Plugin.Extra = nilIfEmpty(prest)
	s.Extra = nilIfEmpty(rest)
	return s, nil
}

// RenderForm writes f as a daemon config.
func RenderForm(f Form) ([]byte, error) {
	doc := copyMap(f.Extra)
	daemon := copyMap(f.Daemon.Extra)
	setPtr(daemon, "port", f.Daemon.Port)
	setPtr(daemon, "lcs", f.Daemon.Lcs)
	setPtr(daemon, "hostname", f.Daemon.Hostname)
	setPtr(daemon, "cert", f.Daemon.Cert)
	setPtr(daemon, "orchestrator", f.Daemon.Orchestrator)
	if len(daemon) > 0 {
		doc["daemon"] = daemon
	}
	if f.Simulator != nil {
		sim := copyMap(f.Simulator.Extra)
		setString(sim, "package", f.Simulator.Package)
		if f.Simulator.Args != nil {
			sim["args"] = f.Simulator.Args
		}
		doc["simulator"] = sim
	}
	if len(f.Services) > 0 {
		services := make([]map[string]any, 0, len(f.Services))
		for _, s := range f.Services {
			svc := copyMap(s.Extra)
			setString(svc, "name", s.Name)
			setString(svc, "type", s.Type)
			setString(svc, "auth", s.Auth)
			if s.Simulate {
				svc["simulate"] = true
			}
			plugin := copyMap(s.Plugin.Extra)
			setString(plugin, "plugin", s.Plugin.Plugin)
			setString(plugin, "package", s.Plugin.Package)
			setString(plugin, "isolation", s.Plugin.Isolation)
			if s.Plugin.Args != nil {
				plugin["args"] = s.Plugin.Args
			}
			svc["plugin"] = plugin
			services = append(services, svc)
		}
		doc["services"] = services
	}
	var buf bytes.Buffer
	if err := toml.NewEncoder(&buf).Encode(doc); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// DecodeFormJSON reads a Form from JSON, keeping integers in args and extra
// keys as integers rather than floats, so a port stays 9000 and not 9000.0.
func DecodeFormJSON(r io.Reader) (Form, error) {
	var f Form
	dec := json.NewDecoder(r)
	dec.UseNumber()
	if err := dec.Decode(&f); err != nil {
		return Form{}, err
	}
	normalizeForm(&f)
	return f, nil
}

// normalizeForm turns the json.Numbers in f's free-form maps into int64s
// and float64s.
func normalizeForm(f *Form) {
	f.Extra = normalizeMap(f.Extra)
	f.Daemon.Extra = normalizeMap(f.Daemon.Extra)
	if f.Simulator != nil {
		f.Simulator.Args = normalizeMap(f.Simulator.Args)
		f.Simulator.Extra = normalizeMap(f.Simulator.Extra)
	}
	for i := range f.Services {
		s := &f.Services[i]
		s.Extra = normalizeMap(s.Extra)
		s.Plugin.Args = normalizeMap(s.Plugin.Args)
		s.Plugin.Extra = normalizeMap(s.Plugin.Extra)
	}
}

func normalizeMap(m map[string]any) map[string]any {
	if m == nil {
		return nil
	}
	out := make(map[string]any, len(m))
	for k, v := range m {
		out[k] = normalize(v)
	}
	return out
}

func normalize(v any) any {
	switch x := v.(type) {
	case json.Number:
		if i, err := x.Int64(); err == nil {
			return i
		}
		f, _ := x.Float64()
		return f
	case map[string]any:
		return normalizeMap(x)
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = normalize(e)
		}
		return out
	}
	return v
}

// findTime returns the dotted path of the first TOML date or time in v, or
// "".
func findTime(v any, path string) string {
	switch x := v.(type) {
	case time.Time:
		return path
	case map[string]any:
		for k, e := range x {
			if p := findTime(e, joinPath(path, k)); p != "" {
				return p
			}
		}
	case []map[string]any:
		for i, e := range x {
			if p := findTime(e, fmt.Sprintf("%s[%d]", path, i)); p != "" {
				return p
			}
		}
	case []any:
		for i, e := range x {
			if p := findTime(e, fmt.Sprintf("%s[%d]", path, i)); p != "" {
				return p
			}
		}
	}
	return ""
}

func joinPath(path, key string) string {
	if path == "" {
		return key
	}
	return path + "." + key
}

func copyMap(m map[string]any) map[string]any {
	out := make(map[string]any, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

func nilIfEmpty(m map[string]any) map[string]any {
	if len(m) == 0 {
		return nil
	}
	return m
}

func take(m map[string]any, key string) (any, bool) {
	v, ok := m[key]
	delete(m, key)
	return v, ok
}

// lastKey is the key part of a dotted path, for looking it up in its table.
func lastKey(path string) string {
	return path[strings.LastIndex(path, ".")+1:]
}

func takeString(m map[string]any, path string) (*string, error) {
	v, ok := take(m, lastKey(path))
	if !ok {
		return nil, nil
	}
	s, ok := v.(string)
	if !ok {
		return nil, unrepresentable("%s is not a string", path)
	}
	return &s, nil
}

func takeInt(m map[string]any, path string) (*int64, error) {
	v, ok := take(m, lastKey(path))
	if !ok {
		return nil, nil
	}
	i, ok := v.(int64)
	if !ok {
		return nil, unrepresentable("%s is not an integer", path)
	}
	return &i, nil
}

func takeTable(m map[string]any, path string) (map[string]any, error) {
	v, ok := take(m, lastKey(path))
	if !ok {
		return nil, nil
	}
	t, ok := v.(map[string]any)
	if !ok {
		return nil, unrepresentable("%s is not a table", path)
	}
	return t, nil
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func setPtr[T any](m map[string]any, key string, v *T) {
	if v != nil {
		m[key] = *v
	}
}

func setString(m map[string]any, key, v string) {
	if v != "" {
		m[key] = v
	}
}
