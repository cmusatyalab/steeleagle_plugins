package mission

import (
	"encoding/json"
	"math"
	"reflect"
	"strings"
	"testing"
)

func schemaField2(t *testing.T, entry any, name string) map[string]any {
	t.Helper()
	for _, f := range entry.(map[string]any)["fields"].([]any) {
		if m := f.(map[string]any); m["name"] == name {
			return m
		}
	}
	t.Fatalf("no field %s", name)
	return nil
}

func eq(t *testing.T, got, want any) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got  %#v\nwant %#v", got, want)
	}
}

func TestBareAndQualifier(t *testing.T) {
	eq(t, Bare("actions.Patrol"), "Patrol")
	eq(t, Bare("params.Polygon_Area_1"), "Polygon_Area_1")
	eq(t, Bare("Patrol"), "Patrol")
	eq(t, Qualifier("enums.AltitudeMode"), "enums")
	eq(t, Qualifier("Patrol"), "")
}

func TestKinds(t *testing.T) {
	ix := exIndex(t)
	for goType, want := range map[string]string{
		"params.Polygon":     "feature",
		"enums.AltitudeMode": "enum",
		"actions.PatrolMode": "enum",
		"datatypes.Pose":     "object",
		"[]float32":          "array",
		"float32":            "number",
		"uint32":             "integer",
		"bool":               "boolean",
		"string":             "string",
		"chan int":           "unknown",
	} {
		if got := ix.Kind(goType); got != want {
			t.Errorf("Kind(%q) = %q, want %q", goType, got, want)
		}
	}
}

func TestKeysAndFieldTypes(t *testing.T) {
	ix := exIndex(t)
	k, ok := ix.Key("actions", "actions.Patrol")
	eq(t, []any{k, ok}, []any{"Patrol", true})
	q, ok := ix.Qualify("actions", "Patrol")
	eq(t, []any{q, ok}, []any{"actions.Patrol", true})
	q, ok = ix.Qualify("events", "Battery")
	eq(t, []any{q, ok}, []any{"events.Battery", true})
	if _, ok := ix.Key("actions", "nope.Nope"); ok {
		t.Error("Key found nope.Nope")
	}
	ft, ok := ix.FieldType("actions.Patrol", "Area")
	eq(t, []any{ft, ok}, []any{"params.Polygon", true})
	ft, ok = ix.FieldType("datatypes.Pose", "Pitch")
	eq(t, []any{ft, ok}, []any{"float32", true})
	if _, ok := ix.FieldType("actions.Patrol", "Nope"); ok {
		t.Error("FieldType found Nope")
	}
}

func TestPatrolSchema(t *testing.T) {
	patrol := RegistryToSchema(exIndex(t))["actions"].(map[string]any)["Patrol"].(map[string]any)

	eq(t, patrol["type"], "actions.Patrol")
	if !strings.HasPrefix(patrol["description"].(string), "Patrol orders the vehicle") {
		t.Errorf("description = %q", patrol["description"])
	}
	fields := patrol["fields"].([]any)
	eq(t, []any{fields[0].(map[string]any)["name"], fields[1].(map[string]any)["name"]}, []any{"Area", "Altitude"})
	eq(t, schemaField2(t, patrol, "Area"), map[string]any{
		"name": "Area", "go_type": "params.Polygon", "type": "string", "required": true,
		"description": "polygon to patrol around", "map_feature": true, "feature_type": "Polygon",
	})
	eq(t, schemaField2(t, patrol, "Altitude")["type"], "number")
	eq(t, schemaField2(t, patrol, "Altitude")["required"], true)
	eq(t, schemaField2(t, patrol, "SurveySpacing")["default"], 10.0)
	eq(t, schemaField2(t, patrol, "SurveySpacing")["required"], false)
	eq(t, schemaField2(t, patrol, "Pattern")["enum_type"], "PatrolMode")
	eq(t, schemaField2(t, patrol, "AltitudeMode")["enum_type"], "AltitudeMode")
	if _, has := schemaField2(t, patrol, "SurveyHeading")["default"]; has {
		t.Error("SurveyHeading has a default")
	}
}

func TestDatatypeFieldsBecomeNestedObjects(t *testing.T) {
	gimbal := RegistryToSchema(exIndex(t))["actions"].(map[string]any)["SetGimbalPose"]
	pose := schemaField2(t, gimbal, "Pose")

	eq(t, pose["type"], "object")
	eq(t, pose["object_type"], "Pose")
	var got [][]any
	for _, f := range pose["nested_fields"].([]any) {
		m := f.(map[string]any)
		got = append(got, []any{m["name"], m["type"], m["required"]})
	}
	eq(t, got, [][]any{{"Pitch", "number", true}, {"Roll", "number", true}, {"Yaw", "number", true}})
}

func TestIntegerDefaultsAreInts(t *testing.T) {
	battery := RegistryToSchema(exIndex(t))["events"].(map[string]any)["Battery"]

	eq(t, schemaField2(t, battery, "Threshold")["type"], "integer")
	eq(t, schemaField2(t, battery, "Threshold")["default"], int64(20))
	eq(t, schemaField2(t, battery, "Comparator")["enum_type"], "Comparator")
}

func TestEnumsExcludeMapFeatures(t *testing.T) {
	enums := RegistryToSchema(exIndex(t))["enums"].(map[string]any)

	eq(t, enums["PatrolMode"].(map[string]any)["values"], []string{"PatrolModeCorridor", "PatrolModeSurvey"})
	eq(t, enums["Comparator"].(map[string]any)["values"], []string{"GreaterOrEqual", "LessOrEqual"})
	eq(t, enums["PatrolMode"].(map[string]any)["type"], "actions.PatrolMode")
	for _, f := range []string{"Polygon", "Point", "LineString", "MultiLineString"} {
		if _, has := enums[f]; has {
			t.Errorf("enums contains %s", f)
		}
	}
}

func TestActionsWithoutOptFields(t *testing.T) {
	land := RegistryToSchema(exIndex(t))["actions"].(map[string]any)["Land"].(map[string]any)

	eq(t, land["fields"], []any{})
}

func TestCollidingBareNamesFallBackToQualifiedKeys(t *testing.T) {
	ix := NewTypeIndex(jsonMap(t, `{
  "actions": {
    "a.Patrol": {"type": "a.Patrol", "fields": []},
    "b.Patrol": {"type": "b.Patrol", "fields": []},
    "a.Land": {"type": "a.Land", "fields": []}
  }, "events": {}, "datatypes": {}, "enums": {}}`))

	var keys []string
	for k := range RegistryToSchema(ix)["actions"].(map[string]any) {
		keys = append(keys, k)
	}
	if len(keys) != 3 {
		t.Fatalf("keys = %v", keys)
	}
	has := map[string]bool{}
	for _, k := range keys {
		has[k] = true
	}
	eq(t, has, map[string]bool{"Land": true, "a.Patrol": true, "b.Patrol": true})
	q, ok := ix.Qualify("actions", "a.Patrol")
	eq(t, []any{q, ok}, []any{"a.Patrol", true})
	if _, ok := ix.Qualify("actions", "Patrol"); ok {
		t.Error("bare Patrol resolved")
	}
}

func TestUnparsableDefaultsAreKeptAsStrings(t *testing.T) {
	for _, value := range []string{"abc", ""} {
		ix := NewTypeIndex(jsonMap(t, `{"actions": {"a.X": {"type": "a.X", "fields": [],
  "optFields": [{"name": "N", "type": "float32", "value": "`+value+`"}]}}}`))
		fields := RegistryToSchema(ix)["actions"].(map[string]any)["X"].(map[string]any)["fields"].([]any)
		got, has := fields[0].(map[string]any)["default"]
		if value == "" {
			if has {
				t.Errorf("empty value gave default %v", got)
			}
		} else {
			eq(t, got, value)
		}
	}
}

func graphToAst(t *testing.T, g Graph) map[string]any {
	t.Helper()
	ast, err := GraphToAst(g, exIndex(t), exHeader(t), nil)
	if err != nil {
		t.Fatal(err)
	}
	return ast
}

func decls(ast map[string]any, stanza string) []any {
	return ast[stanza].(map[string]any)["Decls"].([]any)
}

func TestGraphToAstFullMission(t *testing.T) {
	eq(t, graphToAst(t, exGraph(t)), exAST(t))
}

func TestGraphToAstHasNoRole(t *testing.T) {
	if _, has := graphToAst(t, exGraph(t))["Role"]; has {
		t.Error("Role present")
	}
}

func TestGraphToAstSkipsUnsetParams(t *testing.T) {
	g := exGraph(t)
	g.Nodes = []GraphNode{{InstanceID: "patrol", TypeName: "Patrol", Params: jsonMap(t, `{"Area": null, "Altitude": "", "Speed": 3}`)}}
	g.Edges = nil
	start := "patrol"
	g.StartID = &start

	d := decls(graphToAst(t, g), "Actions")
	eq(t, d[0].(map[string]any)["Attrs"], jsonValue(t, `[{"Key": "Speed", "Value": {"Float": 3}}]`))
}

func TestGraphToAstSkipsUnsetNestedFields(t *testing.T) {
	g := exGraph(t)
	g.Nodes = []GraphNode{{InstanceID: "gimbal", TypeName: "SetGimbalPose", Params: jsonMap(t, `{"Pose": {"Pitch": -10, "Roll": null, "Yaw": ""}}`)}}
	g.Edges = nil

	d := decls(graphToAst(t, g), "Actions")
	attrs := d[0].(map[string]any)["Attrs"].([]any)
	eq(t, attrs[0].(map[string]any)["Value"].(map[string]any)["Inline"].(map[string]any)["Args"],
		jsonValue(t, `[{"Key": "Pitch", "Value": {"Float": -10}}]`))
}

func TestGraphToAstPassesUnknownTypesThrough(t *testing.T) {
	g := exGraph(t)
	g.Nodes = []GraphNode{{InstanceID: "x", TypeName: "Dance", Params: jsonMap(t, `{"Style": "salsa"}`)}}
	g.Edges = nil

	d := decls(graphToAst(t, g), "Actions")
	eq(t, d[0], jsonValue(t, `{"Type": "Dance", "Name": "x", "Attrs": [{"Key": "Style", "Value": {"String": "salsa"}}]}`))
}

func TestGraphToAstWithoutStartHasNoMission(t *testing.T) {
	g := exGraph(t)
	g.StartID = nil
	if _, has := graphToAst(t, g)["Mission"]; has {
		t.Error("Mission present")
	}
}

func TestGraphToAstTreatsNilLikeEmpty(t *testing.T) {
	g := Graph{Nodes: []GraphNode{{InstanceID: "land", TypeName: "Land"}}}

	ast := graphToAst(t, g)

	eq(t, decls(ast, "Actions")[0], jsonValue(t, `{"Type": "actions.Land", "Name": "land", "Attrs": []}`))
	eq(t, decls(ast, "Events"), []any{})
}

func TestNumberKinds(t *testing.T) {
	attrs := func(typeName, params string, event bool) any {
		g := Graph{}
		n := GraphNode{InstanceID: "x", TypeName: typeName, Params: jsonMap(t, params)}
		stanza := "Actions"
		if event {
			g.Events = []GraphEvent{GraphEvent(n)}
			stanza = "Events"
		} else {
			g.Nodes = []GraphNode{n}
		}
		return decls(graphToAst(t, g), stanza)[0].(map[string]any)["Attrs"]
	}

	// a whole number in a float field is still a float literal
	eq(t, attrs("Patrol", `{"Altitude": 15}`, false), jsonValue(t, `[{"Key": "Altitude", "Value": {"Float": 15}}]`))
	// a whole float in an integer field is an int literal
	eq(t, attrs("Battery", `{"Threshold": 15.0}`, true), jsonValue(t, `[{"Key": "Threshold", "Value": {"Int": 15}}]`))
	// a fractional number in an integer field reaches the compiler as-is
	eq(t, attrs("Battery", `{"Threshold": 15.5}`, true), jsonValue(t, `[{"Key": "Threshold", "Value": {"Float": 15.5}}]`))
	// booleans are idents
	eq(t, attrs("Dance", `{"Loud": true}`, false), jsonValue(t, `[{"Key": "Loud", "Value": {"Ident": "true"}}]`))
}

func TestGraphToAstAcceptsPlainGoNumbers(t *testing.T) {
	g := Graph{Nodes: []GraphNode{{InstanceID: "t", TypeName: "TakeOff", Params: map[string]any{"Altitude": 5.5}}}}

	d := decls(graphToAst(t, g), "Actions")

	eq(t, d[0].(map[string]any)["Attrs"], jsonValue(t, `[{"Key": "Altitude", "Value": {"Float": 5.5}}]`))
}

func TestQualifiedEnumValuesAreKept(t *testing.T) {
	g := exGraph(t)
	g.Nodes = []GraphNode{{InstanceID: "p", TypeName: "Patrol", Params: jsonMap(t, `{"Pattern": "actions.PatrolModeCorridor"}`)}}
	g.Edges = nil

	d := decls(graphToAst(t, g), "Actions")
	eq(t, d[0].(map[string]any)["Attrs"], jsonValue(t, `[{"Key": "Pattern", "Value": {"Ident": "actions.PatrolModeCorridor"}}]`))
}

func TestArraysBecomeArrayValues(t *testing.T) {
	ix := NewTypeIndex(jsonMap(t, `{"actions": {"a.Fly": {"type": "a.Fly", "fields": [{"name": "Speeds", "type": "[]float32"}]}}}`))
	g := Graph{Nodes: []GraphNode{{InstanceID: "f", TypeName: "Fly", Params: jsonMap(t, `{"Speeds": [1, 2.5]}`)}}}

	ast, err := GraphToAst(g, ix, exHeader(t), nil)
	if err != nil {
		t.Fatal(err)
	}

	eq(t, decls(ast, "Actions")[0].(map[string]any)["Attrs"],
		jsonValue(t, `[{"Key": "Speeds", "Value": {"Array": {"Elems": [{"Float": 1}, {"Float": 2.5}]}}}]`))
}

func TestObjectForANonObjectFieldIsRejected(t *testing.T) {
	g := exGraph(t)
	g.Nodes = []GraphNode{{InstanceID: "p", TypeName: "Patrol", Params: jsonMap(t, `{"Altitude": {"x": 1}}`)}}
	g.Edges = nil

	_, err := GraphToAst(g, exIndex(t), exHeader(t), nil)

	if err == nil || !strings.Contains(err.Error(), "float32") {
		t.Fatalf("err = %v", err)
	}
}

func TestPrepareGeoJSONPassesValidFeaturesThrough(t *testing.T) {
	data, errs := PrepareGeoJSON(exPolyGeoJSON)

	eq(t, string(data), exPolyGeoJSON)
	eq(t, len(errs), 0)
}

func TestPrepareGeoJSONWithoutFeaturesIsNil(t *testing.T) {
	for _, text := range []string{"", "   ", `{"type": "FeatureCollection", "features": []}`, `{"type": "FeatureCollection", "features": null}`, `{"type": "FeatureCollection"}`} {
		data, errs := PrepareGeoJSON(text)
		if data != nil || len(errs) != 0 {
			t.Errorf("%q: got %v, %v", text, data, errs)
		}
	}
}

func fc(features ...map[string]any) string {
	b, _ := json.Marshal(map[string]any{"type": "FeatureCollection", "features": features})
	return string(b)
}

func feature(name *string, geometry string) map[string]any {
	props := map[string]any{}
	if name != nil {
		props["name"] = *name
	}
	return map[string]any{"type": "Feature", "properties": props,
		"geometry": map[string]any{"type": geometry, "coordinates": []any{}}}
}

func nm(s string) *string { return &s }

func TestPrepareGeoJSONReportsFeaturesEaglecRejects(t *testing.T) {
	for _, tc := range []struct {
		features []map[string]any
		message  string
	}{
		{[]map[string]any{feature(nm("Area B"), "Polygon")}, "Map feature 'Area B' can only contain letters, digits and underscores"},
		{[]map[string]any{feature(nm("north-field"), "Polygon")}, "Map feature 'north-field' can only contain letters, digits and underscores"},
		{[]map[string]any{feature(nil, "Polygon")}, "Map feature 1 has no name"},
		{[]map[string]any{feature(nm(""), "Polygon")}, "Map feature 1 has no name"},
		{[]map[string]any{feature(nm("A"), "Polygon"), feature(nm("A"), "Point")}, "Map feature name 'A' is used more than once"},
		{[]map[string]any{feature(nm("C"), "GeometryCollection")}, "Map feature 'C' is a GeometryCollection, but only Point, LineString, MultiLineString and Polygon features are supported"},
	} {
		_, errs := PrepareGeoJSON(fc(tc.features...))
		eq(t, errs, []string{tc.message})
	}
}

func TestPrepareGeoJSONAcceptsLeadingDigits(t *testing.T) {
	_, errs := PrepareGeoJSON(fc(feature(nm("1st"), "Polygon")))
	eq(t, len(errs), 0)
}

func TestPrepareGeoJSONReportsInvalidJSON(t *testing.T) {
	_, errs := PrepareGeoJSON("{not json")

	if len(errs) != 1 || !strings.HasPrefix(errs[0], "Map features are not valid GeoJSON") {
		t.Fatalf("errs = %v", errs)
	}
}

func TestPrepareGeoJSONReportsMalformedShapesWithoutRaising(t *testing.T) {
	const noGeometry = "Map feature 'A' is a None, but only Point, LineString, MultiLineString and Polygon features are supported"
	for _, tc := range []struct{ text, want string }{
		{`{"type": "FeatureCollection", "features": {"a": 1}}`, "Map features are not valid GeoJSON: features must be a list"},
		{`{"type": "FeatureCollection", "features": "abc"}`, "Map features are not valid GeoJSON: features must be a list"},
		{`{"type": "FeatureCollection", "features": [null]}`, "Map feature 1 is not a valid GeoJSON feature"},
		{`{"type": "FeatureCollection", "features": [5]}`, "Map feature 1 is not a valid GeoJSON feature"},
		{`{"type": "FeatureCollection", "features": ["x"]}`, "Map feature 1 is not a valid GeoJSON feature"},
		{`{"type": "FeatureCollection", "features": [{"type": "Feature", "properties": "x", "geometry": {"type": "Polygon"}}]}`, "Map feature 1 has no name"},
		{`{"type": "FeatureCollection", "features": [{"type": "Feature", "properties": ["a"], "geometry": {"type": "Polygon"}}]}`, "Map feature 1 has no name"},
		{`{"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"name": "A"}, "geometry": "x"}]}`, noGeometry},
		{`{"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"name": "A"}, "geometry": 5}]}`, noGeometry},
	} {
		_, errs := PrepareGeoJSON(tc.text)
		eq(t, errs, []string{tc.want})
	}
}

// normalize round-trips a value through JSON so numbers compare as json.Number.
func normalize(t *testing.T, v any) any {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return jsonValue(t, string(raw))
}

func TestAstToGraphInvertsGraphToAst(t *testing.T) {
	graph := AstToGraph(graphToAst(t, exGraph(t)), exIndex(t))
	want := normalize(t, exGraph(t)).(map[string]any)
	got := normalize(t, graph).(map[string]any)
	for _, key := range []string{"nodes", "events", "edges", "start_id"} {
		eq(t, got[key], want[key])
	}
	eq(t, got["warnings"], []any{})
	eq(t, got["imports"].([]any)[0], map[string]any{"path": sdkModule + "/dsl/actions", "alias": ""})
	eq(t, got["modules"], []any{map[string]any{"path": sdkModule, "version": "", "override": "/repo/sdk"}})
}

func TestAstToGraphAcceptsIntegralFloatsFromEaglec(t *testing.T) {
	// Go marshals Float 15.0 as 15
	ast := jsonMap(t, `{"Actions": {"Decls": [{"Type": "actions.Patrol", "Name": "p",
		"Attrs": [{"Key": "Altitude", "Value": {"Float": 15}}]}]}}`)
	node := AstToGraph(ast, exIndex(t))["nodes"].([]any)[0].(map[string]any)
	params := node["params"].(map[string]any)
	if v, ok := params["Altitude"].(float64); !ok || v != 15 {
		t.Errorf("Altitude = %#v, want float64 15", params["Altitude"])
	}
}

func TestAstToGraphInlinesDataAndWarns(t *testing.T) {
	ast := jsonMap(t, `{
  "Role": {"Name": "Patroller"},
  "Data": {"Decls": [
    {"Type": "datatypes.Pose", "Name": "down", "Attrs": [
      {"Key": "Pitch", "Value": {"Float": -90}},
      {"Key": "Roll", "Value": {"Float": 0}},
      {"Key": "Yaw", "Value": {"Float": 0}}]},
    {"Type": "datatypes.Pose", "Name": "spare"}
  ]},
  "Actions": {"Decls": [
    {"Type": "actions.SetGimbalPose", "Name": "gimbal", "Attrs": [{"Key": "Pose", "Value": {"Ident": "down"}}]},
    {"Type": "custom.Dance", "Name": "dance", "Attrs": [{"Key": "Loud", "Value": {"Ident": "true"}}]},
    {"Type": "Land", "Name": "land"}
  ]},
  "Mission": {"Start": "gimbal", "Blocks": [
    {"Action": "gimbal", "Rules": [{"Event": "done", "Next": "dance"}]}]}
}`)
	graph := normalize(t, AstToGraph(ast, exIndex(t))).(map[string]any)
	eq(t, graph["nodes"], jsonValue(t, `[
  {"instance_id": "gimbal", "type_name": "SetGimbalPose", "params": {"Pose": {"Pitch": -90, "Roll": 0, "Yaw": 0}}},
  {"instance_id": "dance", "type_name": "Dance", "params": {"Loud": true}},
  {"instance_id": "land", "type_name": "Land", "params": {}}]`))
	eq(t, graph["events"], []any{})
	eq(t, graph["edges"], jsonValue(t, `[{"source": "gimbal", "event_id": "done", "target": "dance"}]`))
	eq(t, graph["start_id"], "gimbal")
	eq(t, graph["warnings"], jsonValue(t, `[
  "custom.Dance is not a known action type",
  "Role Patroller is ignored: the GCS compiles missions without a role",
  "Data spare is not used by any action or event and was dropped"]`))
}

func TestAstToGraphWithoutMission(t *testing.T) {
	graph := normalize(t, AstToGraph(map[string]any{}, exIndex(t)))
	eq(t, graph, jsonValue(t, `{"nodes": [], "events": [], "edges": [], "start_id": null,
		"modules": [], "imports": [], "warnings": []}`))
}

func TestAstToGraphAcceptsFloat64Numbers(t *testing.T) {
	ast := map[string]any{"Actions": map[string]any{"Decls": []any{map[string]any{
		"Type": "actions.TakeOff", "Name": "t", "Attrs": []any{map[string]any{
			"Key": "Altitude", "Value": map[string]any{"Float": 5.5}}}}}}}
	node := AstToGraph(ast, exIndex(t))["nodes"].([]any)[0].(map[string]any)
	eq(t, node["params"], map[string]any{"Altitude": 5.5})
}

func astDSL(t *testing.T, ast map[string]any) string {
	t.Helper()
	s, err := AstToDSL(ast)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func TestAstToDSLFullMission(t *testing.T) {
	eq(t, astDSL(t, exAST(t)), exDSL)
}

func TestAstToDSLRendersEveryValueForm(t *testing.T) {
	ast := jsonMap(t, `{
  "Role": {"Name": "Patroller"},
  "Modules": {"Modules": [{"Path": "example.com/m", "Version": "v1.2.3"}]},
  "Imports": {"Imports": [{"Path": "example.com/m/dt", "Alias": "dt"}]},
  "Data": {"Decls": [{"Type": "dt.Pose", "Name": "down", "Attrs": [{"Key": "Pitch", "Value": {"Float": -90}}]}]},
  "Actions": {"Decls": [{"Type": "x.Y", "Name": "y", "Attrs": [
    {"Key": "A", "Value": {"Array": {"Elems": [{"Int": 1}, {"Float": 2.5}, {"String": "x"}]}}},
    {"Key": "S", "Value": {"String": "say \"hi\""}},
    {"Key": "B", "Value": {"Ident": "true"}},
    {"Key": "P", "Value": {"Ident": "down"}}]}]}
}`)
	want := "Role Patroller:\n\n" +
		"Modules:\n" +
		"    \"example.com/m\" v1.2.3\n\n" +
		"Imports:\n" +
		"    dt \"example.com/m/dt\"\n\n" +
		"Data:\n" +
		"    dt.Pose down(Pitch=-90.0)\n\n" +
		"Actions:\n" +
		"    x.Y y(A=[1, 2.5, \"x\"], S='say \"hi\"', B=true, P=down)\n"
	eq(t, astDSL(t, ast), want)
}

func floatAST(f any) map[string]any {
	return map[string]any{"Actions": map[string]any{"Decls": []any{map[string]any{
		"Type": "a.B", "Name": "b", "Attrs": []any{map[string]any{
			"Key": "F", "Value": map[string]any{"Float": f}}}}}}}
}

func TestRenderFloatsNeverUseExponents(t *testing.T) {
	for in, want := range map[any]string{
		0.00001:                            "a.B b(F=0.00001)",
		1e20:                               "a.B b(F=100000000000000000000.0)",
		-3.0:                               "a.B b(F=-3.0)",
		2.5:                                "a.B b(F=2.5)",
		json.Number("1e-05"):               "a.B b(F=0.00001)",
		json.Number("-3"):                  "a.B b(F=-3.0)",
		json.Number("1e+20"):               "a.B b(F=100000000000000000000.0)",
		1e16:                               "a.B b(F=10000000000000000.0)",
		123456789012345680000.0:            "a.B b(F=123456789012345683968.0)",
		json.Number("0.30000000000000004"): "a.B b(F=0.30000000000000004)",
	} {
		got := strings.Split(astDSL(t, floatAST(in)), "\n")[1]
		if got = strings.TrimSpace(got); got != want {
			t.Errorf("render(%v) = %q, want %q", in, got, want)
		}
	}
}

func TestAstToDSLRejectsUnrenderableValues(t *testing.T) {
	str := map[string]any{"String": `both ' and " `}
	ast := map[string]any{"Actions": map[string]any{"Decls": []any{map[string]any{
		"Type": "a.B", "Name": "b", "Attrs": []any{map[string]any{"Key": "V", "Value": str}}}}}}
	if _, err := AstToDSL(ast); err == nil || !strings.Contains(err.Error(), "quote") {
		t.Errorf("err = %v, want quote error", err)
	}
	if _, err := AstToDSL(floatAST(math.NaN())); err == nil || !strings.Contains(err.Error(), "finite") {
		t.Errorf("err = %v, want finite error", err)
	}
}

const legacyWarning = "Converted the old Import: header to Imports:"

func TestUpgradeLegacyDSLRenamesTheImportHeader(t *testing.T) {
	got, warnings := UpgradeLegacyDSL("Import:\n    \"example.com/x\"\nActions:\n    a.B b()\n")
	eq(t, got, "Imports:\n    \"example.com/x\"\nActions:\n    a.B b()\n")
	eq(t, warnings, []string{legacyWarning})
}

func TestUpgradeLegacyDSLToleratesWhitespace(t *testing.T) {
	for header, upgraded := range map[string]string{
		"  Import:": "  Imports:",
		"Import :":  "Imports :",
		"Import:\r": "Imports:\r",
	} {
		got, warnings := UpgradeLegacyDSL("Role X:\n" + header + "\n")
		eq(t, got, "Role X:\n"+upgraded+"\n")
		eq(t, warnings, []string{legacyWarning})
	}
}

func TestUpgradeLegacyDSLLeavesCurrentDSLAlone(t *testing.T) {
	for _, dsl := range []string{
		"Imports:\n    \"example.com/x\"\n",
		"# Import: is the old spelling\nActions:\n",
		"Actions:\n    a.Import Import()\n",
		"",
	} {
		got, warnings := UpgradeLegacyDSL(dsl)
		eq(t, got, dsl)
		if len(warnings) != 0 {
			t.Errorf("warnings = %v for %q", warnings, dsl)
		}
	}
}

func engineIndex(t *testing.T) *TypeIndex {
	t.Helper()
	return NewTypeIndex(jsonMap(t, `{"actions": {"actions.Track": {"type": "actions.Track", "fields": [{"name": "Engine", "type": "params.DetectionEngine"}]}}}`))
}

func engineGraph(t *testing.T, engine string) Graph {
	t.Helper()
	return Graph{Nodes: []GraphNode{{InstanceID: "trk", TypeName: "Track", Params: jsonMap(t, `{"Engine": "`+engine+`"}`)}}}
}

func engineIdent(t *testing.T, ast map[string]any) any {
	t.Helper()
	attrs := decls(ast, "Actions")[0].(map[string]any)["Attrs"].([]any)
	return attrs[0].(map[string]any)["Value"].(map[string]any)["Ident"]
}

func TestSchemaEngineField(t *testing.T) {
	schema := RegistryToSchema(engineIndex(t))

	f := schema["actions"].(map[string]any)["Track"].(map[string]any)["fields"].([]any)[0].(map[string]any)
	eq(t, f["type"], "string")
	eq(t, f["engine_type"], "DetectionEngine")
}

func TestGraphToAstEngineVerbatim(t *testing.T) {
	ast, err := GraphToAst(engineGraph(t, "yolo"), engineIndex(t), exHeader(t), nil)
	if err != nil {
		t.Fatal(err)
	}

	eq(t, engineIdent(t, ast), "DetectionEngine_yolo")
}

func TestGraphToAstEngineResolved(t *testing.T) {
	ast, err := GraphToAst(engineGraph(t, "yolo"), engineIndex(t), exHeader(t), map[string]string{"yolo": "100.64.0.2.yolo"})
	if err != nil {
		t.Fatal(err)
	}

	eq(t, engineIdent(t, ast), "DetectionEngine_100_64_0_2_yolo")
}

func TestGraphToAstEngineUnknown(t *testing.T) {
	_, err := GraphToAst(engineGraph(t, "yolo"), engineIndex(t), exHeader(t), map[string]string{})

	if err == nil || err.Error() != "mission uses engine yolo in trk.Engine, but the flow has no such engine" {
		t.Fatalf("err = %v", err)
	}
}

func TestAstToGraphEngine(t *testing.T) {
	ast := jsonMap(t, `{"Actions": {"Decls": [{"Type": "actions.Track", "Name": "trk", "Attrs": [{"Key": "Engine", "Value": {"Ident": "DetectionEngine_yolo"}}]}]}}`)

	g := AstToGraph(ast, engineIndex(t))

	eq(t, g["nodes"].([]any)[0].(map[string]any)["params"], map[string]any{"Engine": "yolo"})
}
