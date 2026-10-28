package mission

import (
	"encoding/json"
	"fmt"
	"go/token"
	"math"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// FeatureTypes are the params types whose constants are generated from the
// mission's GeoJSON, one per drawn feature: the frontend picks these from the
// Map tab instead of from a fixed enum.
var FeatureTypes = []string{
	"params.Point",
	"params.LineString",
	"params.MultiLineString",
	"params.Polygon",
}

// EngineTypes are the params types whose constants the SDK generates from the
// mission manifest's engines, one per engine: a field of one of these types
// names a flow engine.
var EngineTypes = []string{
	"params.DetectionEngine",
	"params.ClassificationEngine",
	"params.GuidanceEngine",
	"params.PositionEngine",
	"params.GenericEngine",
}

func isEngineType(t string) bool {
	for _, e := range EngineTypes {
		if e == t {
			return true
		}
	}
	return false
}

// constName is how the SDK spells an engine name inside a generated constant
// (sdk/preprocess/const_gen.go constNameReplacer).
func constName(name string) string {
	return strings.NewReplacer(".", "_", "/", "_").Replace(name)
}

func isFeatureType(t string) bool {
	for _, f := range FeatureTypes {
		if f == t {
			return true
		}
	}
	return false
}

var numberTypes = map[string]bool{"float32": true, "float64": true}

var integerTypes = map[string]bool{
	"int": true, "int8": true, "int16": true, "int32": true, "int64": true,
	"uint": true, "uint8": true, "uint16": true, "uint32": true, "uint64": true,
}

// Bare turns 'actions.Patrol' into 'Patrol'.
func Bare(q string) string {
	if i := strings.LastIndex(q, "."); i >= 0 {
		return q[i+1:]
	}
	return q
}

// Qualifier turns 'enums.AltitudeMode' into 'enums'; "" for an unqualified name.
func Qualifier(q string) string {
	if i := strings.LastIndex(q, "."); i >= 0 {
		return q[:i]
	}
	return ""
}

// fieldList returns a registry base's required fields followed by its
// optional ones, as field maps.
func fieldList(base map[string]any, optional bool) []map[string]any {
	key := "fields"
	if optional {
		key = "optFields"
	}
	raw, _ := base[key].([]any)
	out := make([]map[string]any, 0, len(raw))
	for _, f := range raw {
		if m, ok := f.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

func allFields(base map[string]any) []map[string]any {
	return append(fieldList(base, false), fieldList(base, true)...)
}

func str(m map[string]any, key string) string {
	s, _ := m[key].(string)
	return s
}

// section reads one registry section as qualified name -> base.
func section(registry map[string]any, name string) map[string]map[string]any {
	out := map[string]map[string]any{}
	raw, _ := registry[name].(map[string]any)
	for q, b := range raw {
		if m, ok := b.(map[string]any); ok {
			out[q] = m
		} else {
			out[q] = map[string]any{}
		}
	}
	return out
}

// displayKeys maps qualified name to frontend key: the bare name, unless
// another type in the same section shares it, in which case the qualified name.
func displayKeys(bases map[string]map[string]any) map[string]string {
	counts := map[string]int{}
	for q := range bases {
		counts[Bare(q)]++
	}
	keys := make(map[string]string, len(bases))
	for q := range bases {
		if counts[Bare(q)] == 1 {
			keys[q] = Bare(q)
		} else {
			keys[q] = q
		}
	}
	return keys
}

// TypeIndex holds lookups over an eaglec type registry (`eaglec schema`
// output).
type TypeIndex struct {
	actions, events, datatypes, enums map[string]map[string]any
	keys                              map[string]map[string]string
	byKey                             map[string]map[string]string
	fieldTypes                        map[string]map[string]string
	fieldOrder                        map[string][]string
}

// NewTypeIndex indexes a decoded registry.
func NewTypeIndex(registry map[string]any) *TypeIndex {
	ix := &TypeIndex{
		actions:    section(registry, "actions"),
		events:     section(registry, "events"),
		datatypes:  section(registry, "datatypes"),
		enums:      section(registry, "enums"),
		fieldTypes: map[string]map[string]string{},
		fieldOrder: map[string][]string{},
	}
	plainEnums := map[string]map[string]any{}
	for q, b := range ix.enums {
		if !isFeatureType(q) {
			plainEnums[q] = b
		}
	}
	ix.keys = map[string]map[string]string{
		"actions": displayKeys(ix.actions),
		"events":  displayKeys(ix.events),
		"enums":   displayKeys(plainEnums),
	}
	ix.byKey = map[string]map[string]string{}
	for sec, keys := range ix.keys {
		ix.byKey[sec] = map[string]string{}
		for q, k := range keys {
			ix.byKey[sec][k] = q
		}
	}
	for _, sec := range []map[string]map[string]any{ix.actions, ix.events, ix.datatypes} {
		for q, base := range sec {
			types := map[string]string{}
			for _, f := range allFields(base) {
				name := str(f, "name")
				types[name] = str(f, "type")
				ix.fieldOrder[q] = append(ix.fieldOrder[q], name)
			}
			ix.fieldTypes[q] = types
		}
	}
	return ix
}

// Key returns the frontend key of a qualified type in a section ("actions",
// "events" or "enums").
func (ix *TypeIndex) Key(section, qualified string) (string, bool) {
	k, ok := ix.keys[section][qualified]
	return k, ok
}

// Qualify is the inverse of Key.
func (ix *TypeIndex) Qualify(section, key string) (string, bool) {
	q, ok := ix.byKey[section][key]
	return q, ok
}

// FieldType returns the Go type of a field of an action, event or datatype.
func (ix *TypeIndex) FieldType(qualified, field string) (string, bool) {
	t, ok := ix.fieldTypes[qualified][field]
	return t, ok
}

// Kind classifies a Go type: feature, engine, enum, object, array, number, integer,
// boolean, string or unknown.
func (ix *TypeIndex) Kind(goType string) string {
	if isFeatureType(goType) {
		return "feature"
	}
	if isEngineType(goType) {
		return "engine"
	}
	if _, ok := ix.enums[goType]; ok {
		return "enum"
	}
	if _, ok := ix.datatypes[goType]; ok {
		return "object"
	}
	switch {
	case strings.HasPrefix(goType, "[]"):
		return "array"
	case numberTypes[goType]:
		return "number"
	case integerTypes[goType]:
		return "integer"
	case goType == "bool":
		return "boolean"
	case goType == "string":
		return "string"
	}
	return "unknown"
}

// RegistryToSchema builds the /api/schema body the palette and FieldInput
// consume.
func RegistryToSchema(ix *TypeIndex) map[string]any {
	actions := map[string]any{}
	for q, base := range ix.actions {
		actions[ix.keys["actions"][q]] = typeSchema(ix, q, base)
	}
	events := map[string]any{}
	for q, base := range ix.events {
		events[ix.keys["events"][q]] = typeSchema(ix, q, base)
	}
	enums := map[string]any{}
	for q, base := range ix.enums {
		if isFeatureType(q) {
			continue
		}
		values := []string{}
		for _, f := range fieldList(base, false) {
			values = append(values, str(f, "name"))
		}
		enums[ix.keys["enums"][q]] = map[string]any{
			"type":        q,
			"description": str(base, "comment"),
			"values":      values,
		}
	}
	return map[string]any{"actions": actions, "events": events, "enums": enums}
}

func typeSchema(ix *TypeIndex, qualified string, base map[string]any) map[string]any {
	return map[string]any{
		"type":        qualified,
		"description": str(base, "comment"),
		"fields":      schemaFields(ix, base),
	}
}

func schemaFields(ix *TypeIndex, base map[string]any) []any {
	out := []any{}
	for _, f := range fieldList(base, false) {
		out = append(out, schemaField(ix, f, true))
	}
	for _, f := range fieldList(base, true) {
		out = append(out, schemaField(ix, f, false))
	}
	return out
}

func schemaField(ix *TypeIndex, f map[string]any, required bool) map[string]any {
	goType := str(f, "type")
	entry := map[string]any{
		"name":        str(f, "name"),
		"go_type":     goType,
		"required":    required,
		"description": str(f, "comment"),
	}
	kind := ix.Kind(goType)
	switch kind {
	case "feature":
		entry["type"] = "string"
		entry["map_feature"] = true
		entry["feature_type"] = Bare(goType)
	case "engine":
		entry["type"] = "string"
		entry["engine_type"] = Bare(goType)
	case "enum":
		entry["type"] = "string"
		key, _ := ix.Key("enums", goType)
		entry["enum_type"] = key
	case "object":
		entry["type"] = "object"
		entry["object_type"] = Bare(goType)
		entry["nested_fields"] = schemaFields(ix, ix.datatypes[goType])
	case "number", "integer", "boolean", "array":
		entry["type"] = kind
	default:
		entry["type"] = "string"
	}
	if value := str(f, "value"); !required && value != "" {
		entry["default"] = typedDefault(kind, value)
	}
	return entry
}

// typedDefault turns a field's #optional[...] default (always a string in
// the registry) into the JSON type the frontend's inputs hold.
func typedDefault(kind, value string) any {
	switch kind {
	case "number":
		if f, err := strconv.ParseFloat(value, 64); err == nil && !math.IsInf(f, 0) && !math.IsNaN(f) {
			return f
		}
		return value
	case "integer":
		if n, err := strconv.ParseInt(value, 10, 64); err == nil {
			return n
		}
		return value
	case "boolean":
		return value == "true"
	case "enum":
		return Bare(value)
	}
	return value
}

// GraphToAst converts the frontend's graph into eaglec AST JSON, with
// header's Modules: and Imports: and no Role:. Unknown type names pass
// through unqualified so the compiler reports them on their decl. A nil
// Params, Events or Edges is the same as an empty one.
//
// An engine field's value names a flow engine. With a nil engines map the name
// is used as written; otherwise it must be a key of engines, and its value (the
// engine's full resolved name) is what the generated constant names.
func GraphToAst(g Graph, ix *TypeIndex, header map[string]any, engines map[string]string) (map[string]any, error) {
	type block struct {
		action string
		rules  []any
	}
	var blocks []*block
	byAction := map[string]*block{}
	for _, e := range g.Edges {
		b := byAction[e.Source]
		if b == nil {
			b = &block{action: e.Source, rules: []any{}}
			byAction[e.Source] = b
			blocks = append(blocks, b)
		}
		b.rules = append(b.rules, map[string]any{"Event": e.EventID, "Next": e.Target})
	}

	actionDecls := []any{}
	for _, n := range g.Nodes {
		d, err := astDecl(ix, "actions", n, engines)
		if err != nil {
			return nil, err
		}
		actionDecls = append(actionDecls, d)
	}
	eventDecls := []any{}
	for _, e := range g.Events {
		d, err := astDecl(ix, "events", GraphNode(e), engines)
		if err != nil {
			return nil, err
		}
		eventDecls = append(eventDecls, d)
	}
	ast := map[string]any{
		"Modules": header["Modules"],
		"Imports": header["Imports"],
		"Actions": map[string]any{"Decls": actionDecls},
		"Events":  map[string]any{"Decls": eventDecls},
	}
	if g.StartID != nil && *g.StartID != "" {
		bs := []any{}
		for _, b := range blocks {
			bs = append(bs, map[string]any{"Action": b.action, "Rules": b.rules})
		}
		ast["Mission"] = map[string]any{"Start": *g.StartID, "Blocks": bs}
	}
	return ast, nil
}

// orderedParams lists a params object's keys the way the type declares its
// fields (JSON objects decode unordered), with undeclared keys after, sorted.
func orderedParams(ix *TypeIndex, qualified string, params map[string]any) []string {
	keys := make([]string, 0, len(params))
	seen := map[string]bool{}
	for _, name := range ix.fieldOrder[qualified] {
		if _, ok := params[name]; ok && !seen[name] {
			seen[name] = true
			keys = append(keys, name)
		}
	}
	var rest []string
	for k := range params {
		if !seen[k] {
			rest = append(rest, k)
		}
	}
	sort.Strings(rest)
	return append(keys, rest...)
}

func astDecl(ix *TypeIndex, sec string, instance GraphNode, engines map[string]string) (map[string]any, error) {
	qualified, ok := ix.Qualify(sec, instance.TypeName)
	if !ok {
		qualified = instance.TypeName
	}
	attrs := []any{}
	for _, key := range orderedParams(ix, qualified, instance.Params) {
		goType, _ := ix.FieldType(qualified, key)
		value, err := astValue(ix, goType, instance.Params[key], &engineCtx{engines, instance.InstanceID, key})
		if err != nil {
			return nil, err
		}
		if value != nil {
			attrs = append(attrs, map[string]any{"Key": key, "Value": value})
		}
	}
	return map[string]any{"Type": qualified, "Name": instance.InstanceID, "Attrs": attrs}, nil
}

// numberText renders a decoded JSON number as its JSON text.
func numberText(v any) (string, bool) {
	switch n := v.(type) {
	case json.Number:
		return n.String(), true
	case float64:
		return strconv.FormatFloat(n, 'f', -1, 64), true
	case float32:
		return strconv.FormatFloat(float64(n), 'f', -1, 32), true
	case int:
		return strconv.Itoa(n), true
	case int64:
		return strconv.FormatInt(n, 10), true
	case int32:
		return strconv.FormatInt(int64(n), 10), true
	case uint64:
		return strconv.FormatUint(n, 10), true
	}
	return "", false
}

func isIntLiteral(s string) bool { return !strings.ContainsAny(s, ".eE") }

// engineCtx carries what an engine field needs while its value is converted:
// the flow's engines (nil for none known) and the top-level node and field the
// value sits in, for the error.
type engineCtx struct {
	engines map[string]string
	node    string
	field   string
}

// astValue converts one plain-JSON param value into an AST Value, shaped by
// the field's Go type when it's known. It returns nil for an unset value
// (null or "").
func astValue(ix *TypeIndex, goType string, value any, ec *engineCtx) (map[string]any, error) {
	if value == nil || value == "" {
		return nil, nil
	}
	kind := "unknown"
	if goType != "" {
		kind = ix.Kind(goType)
	}
	if kind == "feature" {
		return map[string]any{"Ident": fmt.Sprintf("%s_%v", goType, value)}, nil
	}
	if s, isStr := value.(string); kind == "engine" && isStr {
		full := s
		if ec.engines != nil {
			var ok bool
			if full, ok = ec.engines[s]; !ok {
				return nil, fmt.Errorf("mission uses engine %s in %s.%s, but the flow has no such engine", s, ec.node, ec.field)
			}
		}
		return map[string]any{"Ident": Bare(goType) + "_" + constName(full)}, nil
	}
	if s, isStr := value.(string); kind == "enum" && isStr {
		if strings.Contains(s, ".") {
			return map[string]any{"Ident": s}, nil
		}
		return map[string]any{"Ident": Qualifier(goType) + "." + s}, nil
	}
	if obj, isObj := value.(map[string]any); isObj {
		if kind != "object" {
			t := goType
			if t == "" {
				t = "unknown"
			}
			return nil, fmt.Errorf("an object was given for a field of type %s", t)
		}
		args := []any{}
		for _, key := range orderedParams(ix, goType, obj) {
			ft, _ := ix.FieldType(goType, key)
			nested, err := astValue(ix, ft, obj[key], ec)
			if err != nil {
				return nil, err
			}
			if nested != nil {
				args = append(args, map[string]any{"Key": key, "Value": nested})
			}
		}
		return map[string]any{"Inline": map[string]any{"Type": goType, "Args": args}}, nil
	}
	if b, isBool := value.(bool); isBool {
		if b {
			return map[string]any{"Ident": "true"}, nil
		}
		return map[string]any{"Ident": "false"}, nil
	}
	if text, isNum := numberText(value); isNum {
		f, err := strconv.ParseFloat(text, 64)
		if err != nil {
			return nil, fmt.Errorf("invalid number %q", text)
		}
		switch {
		case kind == "number":
			return map[string]any{"Float": json.Number(strconv.FormatFloat(f, 'f', -1, 64))}, nil
		case kind == "integer" && f == math.Trunc(f):
			if isIntLiteral(text) {
				return map[string]any{"Int": json.Number(text)}, nil
			}
			return map[string]any{"Int": json.Number(strconv.FormatFloat(f, 'f', 0, 64))}, nil
		case isIntLiteral(text):
			return map[string]any{"Int": json.Number(text)}, nil
		}
		return map[string]any{"Float": json.Number(text)}, nil
	}
	if list, isList := value.([]any); isList {
		elemType := ""
		if kind == "array" {
			elemType = goType[2:]
		}
		elems := []any{}
		for _, e := range list {
			v, err := astValue(ix, elemType, e, ec)
			if err != nil {
				return nil, err
			}
			if v != nil {
				elems = append(elems, v)
			}
		}
		return map[string]any{"Array": map[string]any{"Elems": elems}}, nil
	}
	return map[string]any{"String": fmt.Sprint(value)}, nil
}

var geometries = map[string]bool{"Point": true, "LineString": true, "MultiLineString": true, "Polygon": true}

// PrepareGeoJSON checks the Map tab's GeoJSON as eaglec takes it. It returns
// nil data when there are no features, since eaglec rejects an empty file,
// and an error per feature eaglec would reject, naming the feature. It mirrors
// the SDK's geo.parse rules plus the Go-identifier rule for the generated
// params.<Geometry>_<name> constants.
func PrepareGeoJSON(text string) (data []byte, errs []string) {
	if strings.TrimSpace(text) == "" {
		return nil, nil
	}
	var collection any
	if err := json.Unmarshal([]byte(text), &collection); err != nil {
		return nil, []string{"Map features are not valid GeoJSON: " + err.Error()}
	}
	obj, _ := collection.(map[string]any)
	rawFeatures := obj["features"]
	if rawFeatures == nil {
		return nil, nil
	}
	features, ok := rawFeatures.([]any)
	if !ok {
		return nil, []string{"Map features are not valid GeoJSON: features must be a list"}
	}
	if len(features) == 0 {
		return nil, nil
	}
	seen := map[string]bool{}
	for i, f := range features {
		n := i + 1
		feature, ok := f.(map[string]any)
		if !ok {
			errs = append(errs, fmt.Sprintf("Map feature %d is not a valid GeoJSON feature", n))
			continue
		}
		props, _ := feature["properties"].(map[string]any)
		geom, _ := feature["geometry"].(map[string]any)
		name, _ := props["name"].(string)
		if name == "" {
			errs = append(errs, fmt.Sprintf("Map feature %d has no name", n))
			continue
		}
		if seen[name] {
			errs = append(errs, fmt.Sprintf("Map feature name '%s' is used more than once", name))
			continue
		}
		seen[name] = true
		gt, _ := geom["type"].(string)
		if !geometries[gt] {
			shown := "None"
			if g, present := geom["type"]; present && g != nil {
				shown = fmt.Sprint(g)
			}
			errs = append(errs, fmt.Sprintf("Map feature '%s' is a %s, but only Point, LineString, MultiLineString and Polygon features are supported", name, shown))
			continue
		}
		if !token.IsIdentifier(gt + "_" + name) {
			errs = append(errs, fmt.Sprintf("Map feature '%s' can only contain letters, digits and underscores", name))
		}
	}
	return []byte(text), errs
}

// legacyImportHeader is the DSL header the Plan page wrote before the SDK
// renamed it to Imports:
var legacyImportHeader = regexp.MustCompile(`(?m)^(\s*)Import(\s*):`)

// UpgradeLegacyDSL rewrites the pre-rename `Import:` header to `Imports:`, so
// files exported before the SDK grammar change still load, and returns a
// warning saying so (none if nothing changed). Everything else the old exports
// wrote -- bare type names and enum values, string map-feature names -- the
// current grammar already accepts.
func UpgradeLegacyDSL(dsl string) (string, []string) {
	if !legacyImportHeader.MatchString(dsl) {
		return dsl, nil
	}
	return legacyImportHeader.ReplaceAllString(dsl, "${1}Imports${2}:"),
		[]string{"Converted the old Import: header to Imports:"}
}

// asMap, asList and asStr read decoded AST JSON leniently: a missing or
// wrongly typed value is the zero value.
func asMap(v any) map[string]any {
	m, _ := v.(map[string]any)
	return m
}

func asList(v any) []any {
	l, _ := v.([]any)
	return l
}

func asStr(v any) string {
	s, _ := v.(string)
	return s
}

func astDecls(ast map[string]any, stanza string) []any {
	return asList(asMap(ast[stanza])["Decls"])
}

// GraphRoles lists the roles the mission graph declares, for the deployed
// manifest's info.roles. The graph carries no role yet, so it is always nil;
// it is the hook for when it does.
func GraphRoles(g Graph) []string { return nil }

// AstToGraph converts eaglec AST JSON (from `eaglec parse`) into the
// frontend's graph, plus the mission's own modules and imports and warnings
// about anything the canvas can't represent. Data decls referenced by an ident
// are inlined into the params that reference them. Numbers may be json.Number
// or float64; Float values come out as float64 and Int values as int64.
func AstToGraph(ast map[string]any, ix *TypeIndex) map[string]any {
	warnings := []any{}
	data := map[string]map[string]any{}
	var dataOrder []string
	for _, d := range astDecls(ast, "Data") {
		m := asMap(d)
		name := asStr(m["Name"])
		if _, dup := data[name]; !dup {
			dataOrder = append(dataOrder, name)
		}
		data[name] = m
	}
	used := map[string]bool{}

	instances := func(stanza, sec string) []any {
		out := []any{}
		for _, d := range astDecls(ast, stanza) {
			decl := asMap(d)
			key := typeKey(ix, sec, asStr(decl["Type"]), &warnings)
			owner, ok := ix.Qualify(sec, key)
			if !ok {
				owner = asStr(decl["Type"])
			}
			out = append(out, map[string]any{
				"instance_id": asStr(decl["Name"]),
				"type_name":   key,
				"params":      fromAttrs(ix, owner, decl["Attrs"], data, used),
			})
		}
		return out
	}
	nodes := instances("Actions", "actions")
	events := instances("Events", "events")

	mission := asMap(ast["Mission"])
	edges := []any{}
	for _, b := range asList(mission["Blocks"]) {
		block := asMap(b)
		for _, r := range asList(block["Rules"]) {
			rule := asMap(r)
			edges = append(edges, map[string]any{
				"source":   asStr(block["Action"]),
				"event_id": asStr(rule["Event"]),
				"target":   asStr(rule["Next"]),
			})
		}
	}
	if role := asStr(asMap(ast["Role"])["Name"]); role != "" {
		warnings = append(warnings, fmt.Sprintf("Role %s is ignored: the GCS compiles missions without a role", role))
	}
	for _, name := range dataOrder {
		if !used[name] {
			warnings = append(warnings, fmt.Sprintf("Data %s is not used by any action or event and was dropped", name))
		}
	}

	var startID any
	if s := asStr(mission["Start"]); s != "" {
		startID = s
	}
	modules := []any{}
	for _, m := range asList(asMap(ast["Modules"])["Modules"]) {
		mod := asMap(m)
		modules = append(modules, map[string]any{
			"path":     asStr(mod["Path"]),
			"version":  asStr(mod["Version"]),
			"override": asStr(mod["Override"]),
		})
	}
	imports := []any{}
	for _, i := range asList(asMap(ast["Imports"])["Imports"]) {
		imp := asMap(i)
		imports = append(imports, map[string]any{"path": asStr(imp["Path"]), "alias": asStr(imp["Alias"])})
	}
	return map[string]any{
		"nodes":    nodes,
		"events":   events,
		"edges":    edges,
		"start_id": startID,
		"modules":  modules,
		"imports":  imports,
		"warnings": warnings,
	}
}

// typeKey is the frontend key for a decl's type. A bare name the palette knows
// ("Land") is accepted as-is; anything else unknown is warned about and shown
// by its bare name.
func typeKey(ix *TypeIndex, sec, typeName string, warnings *[]any) string {
	if k, ok := ix.Key(sec, typeName); ok {
		return k
	}
	if _, ok := ix.Qualify(sec, typeName); ok {
		return typeName
	}
	*warnings = append(*warnings, fmt.Sprintf("%s is not a known %s type", typeName, strings.TrimSuffix(sec, "s")))
	return Bare(typeName)
}

func fromAttrs(ix *TypeIndex, owner string, attrs any, data map[string]map[string]any, used map[string]bool) map[string]any {
	out := map[string]any{}
	for _, a := range asList(attrs) {
		attr := asMap(a)
		key := asStr(attr["Key"])
		ft, _ := ix.FieldType(owner, key)
		out[key] = fromValue(ix, ft, asMap(attr["Value"]), data, used)
	}
	return out
}

// fromValue converts one AST Value into the plain-JSON param the frontend holds.
func fromValue(ix *TypeIndex, goType string, value map[string]any, data map[string]map[string]any, used map[string]bool) any {
	if v, ok := value["Float"]; ok {
		return toFloat(v)
	}
	if v, ok := value["Int"]; ok {
		return toInt(v)
	}
	if v, ok := value["String"]; ok {
		return asStr(v)
	}
	if v, ok := value["Array"]; ok {
		elemType := ""
		if strings.HasPrefix(goType, "[]") {
			elemType = goType[2:]
		}
		out := []any{}
		for _, e := range asList(asMap(v)["Elems"]) {
			out = append(out, fromValue(ix, elemType, asMap(e), data, used))
		}
		return out
	}
	if v, ok := value["Inline"]; ok {
		inline := asMap(v)
		owner := goType
		if owner == "" {
			owner = asStr(inline["Type"])
		}
		return fromAttrs(ix, owner, inline["Args"], data, used)
	}
	ident := asStr(value["Ident"])
	switch ident {
	case "true":
		return true
	case "false":
		return false
	}
	if d, ok := data[ident]; ok {
		used[ident] = true
		owner := goType
		if owner == "" {
			owner = asStr(d["Type"])
		}
		return fromAttrs(ix, owner, d["Attrs"], data, used)
	}
	if goType != "" && ix.Kind(goType) == "feature" {
		return strings.TrimPrefix(Bare(ident), Bare(goType)+"_")
	}
	if goType != "" && ix.Kind(goType) == "engine" {
		return strings.TrimPrefix(Bare(ident), Bare(goType)+"_")
	}
	return Bare(ident)
}

func toFloat(v any) float64 {
	text, ok := numberText(v)
	if !ok {
		return 0
	}
	f, _ := strconv.ParseFloat(text, 64)
	return f
}

func toInt(v any) int64 {
	text, ok := numberText(v)
	if !ok {
		return 0
	}
	if n, err := strconv.ParseInt(text, 10, 64); err == nil {
		return n
	}
	f, _ := strconv.ParseFloat(text, 64)
	return int64(f)
}

// AstToDSL renders eaglec AST JSON as DSL text, in the grammar of
// sdk/dsl/compiler/parser/ast.go. It fails for a string holding both quote
// characters or a non-finite float, which the DSL can't write.
func AstToDSL(ast map[string]any) (string, error) {
	var parts []string
	if role := asStr(asMap(ast["Role"])["Name"]); role != "" {
		parts = append(parts, "Role "+role+":")
	}
	if modules := asList(asMap(ast["Modules"])["Modules"]); len(modules) > 0 {
		lines := []string{"Modules:"}
		for _, m := range modules {
			line, err := moduleLine(asMap(m))
			if err != nil {
				return "", err
			}
			lines = append(lines, "    "+line)
		}
		parts = append(parts, strings.Join(lines, "\n"))
	}
	if imports := asList(asMap(ast["Imports"])["Imports"]); len(imports) > 0 {
		lines := []string{"Imports:"}
		for _, i := range imports {
			imp := asMap(i)
			path, err := quote(asStr(imp["Path"]))
			if err != nil {
				return "", err
			}
			alias := ""
			if a := asStr(imp["Alias"]); a != "" {
				alias = a + " "
			}
			lines = append(lines, "    "+alias+path)
		}
		parts = append(parts, strings.Join(lines, "\n"))
	}
	for _, stanza := range []string{"Data", "Actions", "Events"} {
		decls := astDecls(ast, stanza)
		if len(decls) == 0 {
			continue
		}
		lines := []string{stanza + ":"}
		for _, d := range decls {
			decl := asMap(d)
			attrs, err := renderAttrs(decl["Attrs"])
			if err != nil {
				return "", err
			}
			lines = append(lines, fmt.Sprintf("    %s %s(%s)", asStr(decl["Type"]), asStr(decl["Name"]), attrs))
		}
		parts = append(parts, strings.Join(lines, "\n"))
	}
	mission := asMap(ast["Mission"])
	if start := asStr(mission["Start"]); start != "" {
		lines := []string{"Mission:", "    Start " + start}
		for _, b := range asList(mission["Blocks"]) {
			block := asMap(b)
			lines = append(lines, "    During "+asStr(block["Action"])+":")
			for _, r := range asList(block["Rules"]) {
				rule := asMap(r)
				lines = append(lines, fmt.Sprintf("        %s -> %s", asStr(rule["Event"]), asStr(rule["Next"])))
			}
		}
		parts = append(parts, strings.Join(lines, "\n"))
	}
	return strings.Join(parts, "\n\n") + "\n", nil
}

func moduleLine(module map[string]any) (string, error) {
	line, err := quote(asStr(module["Path"]))
	if err != nil {
		return "", err
	}
	if v := asStr(module["Version"]); v != "" {
		line += " " + v
	}
	if o := asStr(module["Override"]); o != "" {
		q, err := quote(o)
		if err != nil {
			return "", err
		}
		line += " => " + q
	}
	return line, nil
}

// quote writes a DSL string. The DSL has no escapes, so it is quoted with
// whichever quote character it doesn't contain.
func quote(s string) (string, error) {
	if !strings.Contains(s, `"`) {
		return `"` + s + `"`, nil
	}
	if !strings.Contains(s, `'`) {
		return "'" + s + "'", nil
	}
	return "", fmt.Errorf("can't write a DSL string containing both quote characters: %s", s)
}

func renderAttrs(attrs any) (string, error) {
	var out []string
	for _, a := range asList(attrs) {
		attr := asMap(a)
		v, err := renderValue(asMap(attr["Value"]))
		if err != nil {
			return "", err
		}
		out = append(out, asStr(attr["Key"])+"="+v)
	}
	return strings.Join(out, ", "), nil
}

func renderValue(value map[string]any) (string, error) {
	if v, ok := value["Float"]; ok {
		return renderFloat(toFloat(v))
	}
	if v, ok := value["Int"]; ok {
		if text, ok := numberText(v); ok && isIntLiteral(text) {
			return text, nil
		}
		return strconv.FormatInt(toInt(v), 10), nil
	}
	if v, ok := value["String"]; ok {
		return quote(asStr(v))
	}
	if v, ok := value["Array"]; ok {
		var elems []string
		for _, e := range asList(asMap(v)["Elems"]) {
			s, err := renderValue(asMap(e))
			if err != nil {
				return "", err
			}
			elems = append(elems, s)
		}
		return "[" + strings.Join(elems, ", ") + "]", nil
	}
	if v, ok := value["Inline"]; ok {
		inline := asMap(v)
		attrs, err := renderAttrs(inline["Args"])
		if err != nil {
			return "", err
		}
		return asStr(inline["Type"]) + "(" + attrs + ")", nil
	}
	return asStr(value["Ident"]), nil
}

// renderFloat writes a float in the DSL's Float token form (-?digits.digits):
// always a decimal point, never an exponent. It matches the Python backend's
// output: the shortest round-trip digits where Python's repr has no exponent
// (decimal exponent in [-4, 16)), else 20 decimal places with trailing zeros
// trimmed.
func renderFloat(f float64) (string, error) {
	if math.IsNaN(f) || math.IsInf(f, 0) {
		return "", fmt.Errorf("can't write a non-finite float in the DSL: %v", f)
	}
	s := strconv.FormatFloat(f, 'f', -1, 64)
	if f != 0 {
		if exp := int(math.Floor(math.Log10(math.Abs(f)))); exp < -4 || exp >= 16 {
			// Log10 can be off by one at a power of ten; ask for the exponent itself
			e := strconv.FormatFloat(f, 'e', -1, 64)
			exp, _ = strconv.Atoi(e[strings.IndexByte(e, 'e')+1:])
			if exp < -4 || exp >= 16 {
				s = strings.TrimRight(strconv.FormatFloat(f, 'f', 20, 64), "0")
			}
		}
	}
	if strings.HasSuffix(s, ".") {
		s += "0"
	}
	if !strings.Contains(s, ".") {
		s += ".0"
	}
	return s, nil
}
