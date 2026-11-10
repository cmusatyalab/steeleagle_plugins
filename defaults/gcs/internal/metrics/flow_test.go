package metrics

import (
	"reflect"
	"testing"
)

const (
	fakeDrone = fermiIP + ".fake-drone"
	encoder   = fermiIP + ".encoder"
	gcs       = lambda2IP + ".gcs"
)

// gcsFlow is the GCS flow: the camera through the encoder, and telemetry,
// both to the GCS.
const gcsFlow = `
[[producers]]
name = "rgb"
tokens = 3

[[producers]]
name = "telemetry"

[[engines]]
name = "100.101.84.40.encoder"
location = "local"

[[engines]]
name = "128.2.208.19.gcs"
location = "remote"

[[edges]]
from = "rgb"
to = "100.101.84.40.encoder"
timeout = "1s"

[[edges]]
from = "100.101.84.40.encoder"
to = "128.2.208.19.gcs"
timeout = "1s"

[[edges]]
from = "telemetry"
to = "128.2.208.19.gcs"
timeout = "1s"
`

var testHostnames = map[string]string{fermiIP: "fermi", lambda2IP: "lambda2"}

func busy(fraction float64) *EngineMetrics {
	return &EngineMetrics{Busy: &fraction, Skipped: ptr(0)}
}

func ptr(f float64) *float64 { return &f }

func nodeByID(t *testing.T, v View, id string) Node {
	t.Helper()
	for _, n := range v.Nodes {
		if n.ID == id {
			return n
		}
	}
	t.Fatalf("no node %q in %+v", id, v.Nodes)
	return Node{}
}

func bottlenecks(v View) []string {
	var marked []string
	for _, n := range v.Nodes {
		if n.Bottleneck {
			marked = append(marked, n.ID)
		}
	}
	return marked
}

func TestBuildViewMakesNodesAndEdgesWithHosts(t *testing.T) {
	v, err := BuildView([]byte(gcsFlow), fakeDrone, testHostnames)
	if err != nil {
		t.Fatal(err)
	}

	want := []Node{
		{ID: "rgb", Kind: KindProducer, Label: "rgb", Host: "fermi"},
		{ID: "telemetry", Kind: KindProducer, Label: "telemetry", Host: "fermi"},
		{ID: encoder, Kind: KindEngine, Label: "encoder", Host: "fermi"},
		{ID: gcs, Kind: KindEngine, Label: "gcs", Host: "lambda2"},
	}
	if !reflect.DeepEqual(v.Nodes, want) {
		t.Errorf("nodes = %+v, want %+v", v.Nodes, want)
	}
	wantEdges := []Edge{
		{From: "rgb", To: encoder},
		{From: encoder, To: gcs, CrossesNetwork: true},
		{From: "telemetry", To: gcs, CrossesNetwork: true},
	}
	if !reflect.DeepEqual(v.Edges, wantEdges) {
		t.Errorf("edges = %+v, want %+v", v.Edges, wantEdges)
	}
}

func TestBuildViewMarksTheEdgesThatCrossTheNetwork(t *testing.T) {
	v, err := BuildView([]byte(gcsFlow), fakeDrone, testHostnames)
	if err != nil {
		t.Fatal(err)
	}

	want := map[string]bool{
		"rgb->" + encoder:    false, // producer to a local engine
		encoder + "->" + gcs: true,  // local engine to a remote engine
		"telemetry->" + gcs:  true,  // producer to a remote engine
	}
	for _, e := range v.Edges {
		key := e.From + "->" + e.To
		if got, ok := want[key]; !ok || e.CrossesNetwork != got {
			t.Errorf("edge %s crosses the network = %v, want %v", key, e.CrossesNetwork, got)
		}
	}
}

func TestBuildViewFallsBackToTheIPForAnUnknownHost(t *testing.T) {
	v, err := BuildView([]byte(gcsFlow), fakeDrone, map[string]string{})
	if err != nil {
		t.Fatal(err)
	}
	if got := nodeByID(t, v, gcs).Host; got != lambda2IP {
		t.Errorf("host = %q, want %q", got, lambda2IP)
	}
}

func TestBuildViewOfAnEmptyFlowHasNoNodes(t *testing.T) {
	v, err := BuildView(nil, fakeDrone, testHostnames)
	if err != nil || len(v.Nodes) != 0 || len(v.Edges) != 0 {
		t.Errorf("BuildView(empty) = %+v, %v; want no nodes", v, err)
	}
}

func TestBottleneckIsTheBusiestEngineOnEachPath(t *testing.T) {
	v, _ := BuildView([]byte(gcsFlow), fakeDrone, testHostnames)
	setMetrics(&v, map[string]*EngineMetrics{encoder: busy(0.98), gcs: busy(0.002)})

	MarkBottlenecks(&v)

	if got := bottlenecks(v); !reflect.DeepEqual(got, []string{encoder}) {
		t.Errorf("bottlenecks = %v, want only the encoder", got)
	}
}

func TestIdleSlowEngineIsNotABottleneck(t *testing.T) {
	v, _ := BuildView([]byte(gcsFlow), fakeDrone, testHostnames)
	setMetrics(&v, map[string]*EngineMetrics{encoder: busy(0.1), gcs: busy(0.01)})

	MarkBottlenecks(&v)

	if got := bottlenecks(v); got != nil {
		t.Errorf("bottlenecks = %v, want none", got)
	}
}

func TestEngineThatSkipsIsMarkedBelowTheBusyThreshold(t *testing.T) {
	v, _ := BuildView([]byte(gcsFlow), fakeDrone, testHostnames)
	skipping := busy(0.5)
	skipping.Skipped = ptr(0.3)
	setMetrics(&v, map[string]*EngineMetrics{encoder: skipping, gcs: busy(0.01)})

	MarkBottlenecks(&v)

	if got := bottlenecks(v); !reflect.DeepEqual(got, []string{encoder}) {
		t.Errorf("bottlenecks = %v, want the encoder", got)
	}
}

func TestDiamondMarksABottleneckPerPath(t *testing.T) {
	diamond := `
[[producers]]
name = "camera"
[[engines]]
name = "10.0.0.1.a"
location = "local"
[[engines]]
name = "10.0.0.1.b"
location = "local"
[[engines]]
name = "10.0.0.1.c"
location = "local"
[[edges]]
from = "camera"
to = "10.0.0.1.a"
timeout = "1s"
[[edges]]
from = "camera"
to = "10.0.0.1.b"
timeout = "1s"
[[edges]]
from = "10.0.0.1.a"
to = "10.0.0.1.c"
timeout = "1s"
[[edges]]
from = "10.0.0.1.b"
to = "10.0.0.1.c"
timeout = "1s"
`
	v, err := BuildView([]byte(diamond), "10.0.0.1.drone", nil)
	if err != nil {
		t.Fatal(err)
	}
	setMetrics(&v, map[string]*EngineMetrics{"10.0.0.1.a": busy(0.9), "10.0.0.1.b": busy(0.95), "10.0.0.1.c": busy(0.1)})

	MarkBottlenecks(&v)

	if got := bottlenecks(v); !reflect.DeepEqual(got, []string{"10.0.0.1.a", "10.0.0.1.b"}) {
		t.Errorf("bottlenecks = %v, want a and b", got)
	}
}

func TestEngineWithoutMetricsIsNeverABottleneck(t *testing.T) {
	v, _ := BuildView([]byte(gcsFlow), fakeDrone, testHostnames)

	MarkBottlenecks(&v)

	if got := bottlenecks(v); got != nil {
		t.Errorf("bottlenecks = %v, want none", got)
	}
}

func setMetrics(v *View, byID map[string]*EngineMetrics) {
	for i := range v.Nodes {
		v.Nodes[i].Metrics = byID[v.Nodes[i].ID]
	}
}
