package mission

import (
	"bytes"
	"encoding/json"
	"os"
	"testing"
)

// Shared fixtures for the translation and route tests, ported from the Python
// backend's translate_examples.py. The exported-looking Python names map to
// exHeader, exGraph, exAST, exDSL, exPolyGeoJSON and exIndex. Fixtures that
// tests may modify are functions returning a fresh copy.

const sdkModule = "github.com/cmusatyalab/steeleagle/sdk"

// jsonValue decodes JSON text with numbers as json.Number.
func jsonValue(t testing.TB, text string) any {
	t.Helper()
	dec := json.NewDecoder(bytes.NewReader([]byte(text)))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		t.Fatalf("decode %q: %v", text, err)
	}
	return v
}

func jsonMap(t testing.TB, text string) map[string]any {
	t.Helper()
	m, ok := jsonValue(t, text).(map[string]any)
	if !ok {
		t.Fatalf("%q is not a JSON object", text)
	}
	return m
}

// jsonGraph decodes a Graph with numbers as json.Number.
func jsonGraph(t testing.TB, text string) Graph {
	t.Helper()
	dec := json.NewDecoder(bytes.NewReader([]byte(text)))
	dec.UseNumber()
	var g Graph
	if err := dec.Decode(&g); err != nil {
		t.Fatalf("decode graph: %v", err)
	}
	return g
}

// exIndex is the TypeIndex over the recorded `eaglec schema` registry.
func exIndex(t testing.TB) *TypeIndex {
	t.Helper()
	raw, err := os.ReadFile("testdata/registry.json")
	if err != nil {
		t.Fatal(err)
	}
	return NewTypeIndex(jsonMap(t, string(raw)))
}

func exHeader(t testing.TB) map[string]any {
	t.Helper()
	return jsonMap(t, `{
  "Modules": {"Modules": [{"Path": "`+sdkModule+`", "Override": "/repo/sdk"}]},
  "Imports": {"Imports": [
    {"Path": "`+sdkModule+`/dsl/actions"},
    {"Path": "`+sdkModule+`/dsl/events"},
    {"Path": "`+sdkModule+`/dsl/datatypes"},
    {"Path": "`+sdkModule+`/params"},
    {"Path": "`+sdkModule+`/enums"}
  ]}
}`)
}

func exGraph(t testing.TB) Graph {
	t.Helper()
	return jsonGraph(t, `{
  "nodes": [
    {"instance_id": "takeoff", "type_name": "TakeOff", "params": {"Altitude": 5}},
    {"instance_id": "gimbal", "type_name": "SetGimbalPose",
     "params": {"Pose": {"Pitch": -30, "Roll": 0, "Yaw": 0}, "AngleMode": "AngleModeAbsolute"}},
    {"instance_id": "patrol", "type_name": "Patrol",
     "params": {"Area": "Poly", "Altitude": 15, "Pattern": "PatrolModeSurvey"}},
    {"instance_id": "land", "type_name": "Land", "params": {}}
  ],
  "events": [
    {"instance_id": "low", "type_name": "Battery", "params": {"Comparator": "LessOrEqual", "Threshold": 15}}
  ],
  "edges": [
    {"source": "takeoff", "event_id": "done", "target": "gimbal"},
    {"source": "gimbal", "event_id": "done", "target": "patrol"},
    {"source": "patrol", "event_id": "low", "target": "land"},
    {"source": "patrol", "event_id": "done", "target": "land"}
  ],
  "start_id": "takeoff"
}`)
}

// exAST is the AST for exGraph under exHeader. Floats are written in the
// canonical form GraphToAst produces (5, not 5.0), like Go's JSON encoding.
func exAST(t testing.TB) map[string]any {
	t.Helper()
	ast := jsonMap(t, `{
  "Actions": {"Decls": [
    {"Type": "actions.TakeOff", "Name": "takeoff", "Attrs": [{"Key": "Altitude", "Value": {"Float": 5}}]},
    {"Type": "actions.SetGimbalPose", "Name": "gimbal", "Attrs": [
      {"Key": "Pose", "Value": {"Inline": {"Type": "datatypes.Pose", "Args": [
        {"Key": "Pitch", "Value": {"Float": -30}},
        {"Key": "Roll", "Value": {"Float": 0}},
        {"Key": "Yaw", "Value": {"Float": 0}}
      ]}}},
      {"Key": "AngleMode", "Value": {"Ident": "enums.AngleModeAbsolute"}}
    ]},
    {"Type": "actions.Patrol", "Name": "patrol", "Attrs": [
      {"Key": "Area", "Value": {"Ident": "params.Polygon_Poly"}},
      {"Key": "Altitude", "Value": {"Float": 15}},
      {"Key": "Pattern", "Value": {"Ident": "actions.PatrolModeSurvey"}}
    ]},
    {"Type": "actions.Land", "Name": "land", "Attrs": []}
  ]},
  "Events": {"Decls": [
    {"Type": "events.Battery", "Name": "low", "Attrs": [
      {"Key": "Comparator", "Value": {"Ident": "events.LessOrEqual"}},
      {"Key": "Threshold", "Value": {"Int": 15}}
    ]}
  ]},
  "Mission": {"Start": "takeoff", "Blocks": [
    {"Action": "takeoff", "Rules": [{"Event": "done", "Next": "gimbal"}]},
    {"Action": "gimbal", "Rules": [{"Event": "done", "Next": "patrol"}]},
    {"Action": "patrol", "Rules": [{"Event": "low", "Next": "land"}, {"Event": "done", "Next": "land"}]}
  ]}
}`)
	for k, v := range exHeader(t) {
		ast[k] = v
	}
	return ast
}

const exPolyGeoJSON = `{"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"name": "Poly"}, "geometry": {"type": "Polygon", "coordinates": [[[-79.9468, 40.4128], [-79.9463, 40.4133], [-79.9458, 40.413], [-79.9463, 40.4124], [-79.9468, 40.4128]]]}}]}`

const exDSL = `Modules:
    "github.com/cmusatyalab/steeleagle/sdk" => "/repo/sdk"

Imports:
    "github.com/cmusatyalab/steeleagle/sdk/dsl/actions"
    "github.com/cmusatyalab/steeleagle/sdk/dsl/events"
    "github.com/cmusatyalab/steeleagle/sdk/dsl/datatypes"
    "github.com/cmusatyalab/steeleagle/sdk/params"
    "github.com/cmusatyalab/steeleagle/sdk/enums"

Actions:
    actions.TakeOff takeoff(Altitude=5.0)
    actions.SetGimbalPose gimbal(Pose=datatypes.Pose(Pitch=-30.0, Roll=0.0, Yaw=0.0), AngleMode=enums.AngleModeAbsolute)
    actions.Patrol patrol(Area=params.Polygon_Poly, Altitude=15.0, Pattern=actions.PatrolModeSurvey)
    actions.Land land()

Events:
    events.Battery low(Comparator=events.LessOrEqual, Threshold=15)

Mission:
    Start takeoff
    During takeoff:
        done -> gimbal
    During gimbal:
        done -> patrol
    During patrol:
        low -> land
        done -> land
`
