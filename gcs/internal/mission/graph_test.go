package mission

import (
	"reflect"
	"strings"
	"testing"
)

func sp(s string) *string { return &s }

func testGraph() Graph {
	return Graph{
		Nodes: []GraphNode{
			{InstanceID: "takeoff", TypeName: "actions.Takeoff"},
			{InstanceID: "patrol", TypeName: "actions.Patrol"},
		},
		Events: []GraphEvent{{InstanceID: "low", TypeName: "events.LowBattery"}},
		Edges: []GraphEdge{
			{Source: "takeoff", EventID: "done", Target: "patrol"},
			{Source: "patrol", EventID: "low", Target: "takeoff"},
		},
		StartID: sp("takeoff"),
	}
}

func TestStructuralErrorsValid(t *testing.T) {
	if errs := StructuralErrors(testGraph()); len(errs) != 0 {
		t.Fatalf("want no errors, got %v", errs)
	}
}

func TestStructuralErrorsNoStart(t *testing.T) {
	g := testGraph()
	g.StartID = nil
	want := []GraphError{{Message: "No start state: right-click a node and set it as the start state"}}
	if got := StructuralErrors(g); !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
}

func TestStructuralErrorsDanglingStart(t *testing.T) {
	g := testGraph()
	g.StartID = sp("zzz")
	want := []GraphError{{NodeID: sp("zzz"), Message: "start_id 'zzz' does not refer to any node"}}
	if got := StructuralErrors(g); !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
}

func TestStructuralErrorsDanglingEdgesAndDuplicates(t *testing.T) {
	g := testGraph()
	g.Nodes = append(g.Nodes, g.Nodes[0])
	g.Edges = []GraphEdge{{Source: "patrol", EventID: "nope", Target: "ghost"}}
	var msgs []string
	for _, e := range StructuralErrors(g) {
		msgs = append(msgs, e.Message)
	}
	want := []string{
		"Duplicate node instance_id: 'takeoff'",
		"Edge target 'ghost' does not refer to any node",
		"Edge event_id 'nope' does not refer to any declared event",
	}
	if !reflect.DeepEqual(msgs, want) {
		t.Fatalf("got %v want %v", msgs, want)
	}
}

func TestStructuralErrorsEdgeSourceDangling(t *testing.T) {
	g := testGraph()
	g.Edges = []GraphEdge{{Source: "ghost", EventID: "done", Target: "patrol"}}
	want := []GraphError{{NodeID: sp("ghost"), Message: "Edge source 'ghost' does not refer to any node"}}
	if got := StructuralErrors(g); !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
}

func TestStructuralErrorsNonIdentifierNodeNames(t *testing.T) {
	g := testGraph()
	g.Nodes[0].InstanceID = "land here"
	g.StartID = sp("land here")
	g.Edges = nil
	want := GraphError{
		NodeID:  sp("land here"),
		Message: "Node name 'land here' can only contain letters, digits and underscores, and can't start with a digit",
	}
	got := StructuralErrors(g)
	if !reflect.DeepEqual(got, []GraphError{want}) {
		t.Fatalf("got %v", got)
	}
}

func TestStructuralErrorsNonIdentifierEventNames(t *testing.T) {
	g := testGraph()
	g.Events[0].InstanceID = "1st-event"
	g.Edges = nil
	want := []GraphError{{
		EventID: sp("1st-event"),
		Message: "Event name '1st-event' can only contain letters, digits and underscores, and can't start with a digit",
	}}
	if got := StructuralErrors(g); !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
}

func TestStructuralErrorsReservedDone(t *testing.T) {
	g := testGraph()
	g.Events[0].InstanceID = "done"
	g.Edges = nil
	want := []GraphError{{
		EventID: sp("done"),
		Message: "Event name 'done' is reserved for the action-finished transition",
	}}
	if got := StructuralErrors(g); !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
}

func TestStructuralErrorsGoKeywordRejected(t *testing.T) {
	g := testGraph()
	g.Nodes[1].InstanceID = "func"
	g.Edges = nil
	got := StructuralErrors(g)
	if len(got) != 1 || !strings.HasPrefix(got[0].Message, "Node name 'func'") {
		t.Fatalf("got %v", got)
	}
}

func TestStructuralErrorsEventIDsCarried(t *testing.T) {
	g := testGraph()
	g.Events = append(g.Events, g.Events[0])
	g.Edges = []GraphEdge{{Source: "patrol", EventID: "nope", Target: "takeoff"}}
	by := map[string]GraphError{}
	for _, e := range StructuralErrors(g) {
		by[e.Message] = e
	}
	dup := by["Duplicate event instance_id: 'low'"]
	if dup.NodeID != nil || dup.EventID == nil || *dup.EventID != "low" {
		t.Fatalf("duplicate: %+v", dup)
	}
	dang := by["Edge event_id 'nope' does not refer to any declared event"]
	if dang.NodeID != nil || dang.EventID == nil || *dang.EventID != "nope" {
		t.Fatalf("dangling: %+v", dang)
	}
}

func TestMapErrors(t *testing.T) {
	errs := []DeclError{
		{Decl: "patrol", Message: "no field Altitud"},
		{Decl: "low", Message: "bad comparator"},
		{Decl: "", Message: "go build failed"},
	}
	want := []GraphError{
		{NodeID: sp("patrol"), Message: "no field Altitud"},
		{EventID: sp("low"), Message: "bad comparator"},
		{Message: "go build failed"},
	}
	if got := MapErrors(errs, testGraph()); !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
}

func TestValidName(t *testing.T) {
	for _, ok := range []string{"survey_north", "a", "m-1.v2", strings.Repeat("a", 64)} {
		if err := ValidName(ok); err != nil {
			t.Errorf("%q: unexpected error %v", ok, err)
		}
	}
	for _, bad := range []string{"", ".hidden", "a b", "a/b", "..", strings.Repeat("a", 65), "é"} {
		err := ValidName(bad)
		if err == nil {
			t.Errorf("%q: want error", bad)
		} else if err.Error() != "mission name must be 1-64 letters, digits, '_', '-' or '.', not starting with '.'" {
			t.Errorf("%q: message %q", bad, err)
		}
	}
}
