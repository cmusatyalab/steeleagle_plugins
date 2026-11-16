package metrics

import (
	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"strings"
)

// Node kinds.
const (
	KindProducer = "producer"
	KindEngine   = "engine"
)

// States of a view.
const (
	StateOK                    = "ok"
	StateNoFlow                = "no_flow"
	StatePrometheusUnreachable = "prometheus_unreachable"
)

// The busiest engine on a path is marked the bottleneck when it is busy at
// least BottleneckBusyFraction of the window, or skips at least
// BottleneckSkippedShare of its inputs. A few skips don't count: an idle
// engine with several inputs still skips one now and then, when a burst
// replaces an input before the engine takes it.
const (
	BottleneckBusyFraction = 0.8
	BottleneckSkippedShare = 0.05
)

// EngineMetrics are an engine's numbers over a window. Each is null when
// Prometheus has no sample for it in the window. Mean is the exact mean time
// per input; P50 and P95 are estimated from the histogram's buckets, so only
// tell which bucket the time falls in when the buckets are wide.
type EngineMetrics struct {
	Mean            *float64 `json:"mean_seconds"`
	P50             *float64 `json:"p50_seconds"`
	P95             *float64 `json:"p95_seconds"`
	Wait            *float64 `json:"wait_seconds"`
	InputsPerSecond *float64 `json:"inputs_per_second"`
	Skipped         *float64 `json:"skipped_share"`
	Busy            *float64 `json:"busy_fraction"`
}

// Latency is a time's exact mean, and its median and 95th percentile as
// estimated from its histogram's buckets, each null without data.
type Latency struct {
	Mean *float64 `json:"mean_seconds"`
	P50  *float64 `json:"p50_seconds"`
	P95  *float64 `json:"p95_seconds"`
}

// Node is a producer or engine of a flow. An engine's ID is its qualified
// name, which is also the engine_id its metrics are labeled with.
type Node struct {
	ID         string         `json:"id"`
	Kind       string         `json:"kind"`
	Label      string         `json:"label"`
	Host       string         `json:"host"`
	Metrics    *EngineMetrics `json:"metrics"`
	Bottleneck bool           `json:"bottleneck"`
}

// Edge leads from a producer or engine to an engine. It crosses the network
// when one end runs on the vehicle and the other on the remote server.
type Edge struct {
	From           string `json:"from"`
	To             string `json:"to"`
	CrossesNetwork bool   `json:"crosses_network"`
}

// Network is the round trip time between the vehicle's daemon and its remote
// server over a window, as its QUIC connection estimates it, each null
// without samples. The minimum approaches the round trip propagation delay;
// the mean adds the queueing on the way.
type Network struct {
	RTTMin  *float64 `json:"rtt_min_seconds"`
	RTTMean *float64 `json:"rtt_mean_seconds"`
}

// View is a vehicle's running flow, with the numbers of a window.
type View struct {
	Vehicle  string   `json:"vehicle"`
	Window   string   `json:"window"`
	State    string   `json:"state"`
	Detail   string   `json:"detail,omitempty"`
	Nodes    []Node   `json:"nodes"`
	Edges    []Edge   `json:"edges"`
	EndToEnd *Latency `json:"end_to_end"`
	// ClockSkewShare is the share of the vehicle's frames that reached the
	// GCS before their capture time, by the two hosts' clocks, when any did:
	// the end-to-end time then leaves those frames out and is biased low, or
	// missing if every frame was.
	ClockSkewShare *float64 `json:"clock_skew_share"`
	// Network is null when no edge crosses the network, or there are no
	// round trip samples in the window.
	Network *Network `json:"network"`
}

// BuildView makes the view of flowTOML, the flow vehicle runs, without
// numbers. A flow names its nodes relative to the vehicle (see
// fleet/flownames.go); each node's ID is the name the vehicle qualifies it to,
// which Uriel's metrics label it by: the vehicle's streams and its local
// engines (and their producers) are on the vehicle's daemon, its remote
// engines on the orchestrator's, orchestratorIP. Each node's host is the
// hostname of its daemon, from hostnames by IP, falling back to the IP. An
// empty flowTOML makes a view without nodes.
func BuildView(flowTOML []byte, vehicle, orchestratorIP string, hostnames map[string]string) (View, error) {
	v := View{Vehicle: vehicle, Nodes: []Node{}, Edges: []Edge{}}
	if len(flowTOML) == 0 {
		return v, nil
	}
	flow, err := config.ParseFlowConfigFromBytes(flowTOML)
	if err != nil {
		return View{}, err
	}
	host := func(ip string) string {
		if name, ok := hostnames[ip]; ok && name != "" {
			return name
		}
		return ip
	}
	vehicleIP, _ := fleet.SplitName(vehicle)
	id := func(name string) string { return fleet.QualifyFlowName(name, vehicle, vehicleIP, orchestratorIP) }
	// The node's label, its name without its location prefix, and the IP of
	// the daemon it runs on
	place := func(name string) (label, ip string) {
		for _, prefix := range []string{fleet.VehiclePrefix, fleet.LocalPrefix} {
			if rest, ok := strings.CutPrefix(name, prefix); ok {
				return rest, vehicleIP
			}
		}
		if rest, ok := strings.CutPrefix(name, fleet.RemotePrefix); ok {
			return rest, orchestratorIP
		}
		return name, vehicleIP
	}
	// Where each node runs, by ID: producers on the vehicle, engines where the
	// flow places them
	location := make(map[string]string, len(flow.Producers)+len(flow.Engines))
	for _, p := range flow.Producers {
		label, ip := place(p.Name)
		v.Nodes = append(v.Nodes, Node{ID: id(p.Name), Kind: KindProducer, Label: label, Host: host(ip)})
		location[id(p.Name)] = config.LocationLocal
	}
	for _, e := range flow.Engines {
		label, ip := place(e.Name)
		v.Nodes = append(v.Nodes, Node{ID: id(e.Name), Kind: KindEngine, Label: label, Host: host(ip)})
		location[id(e.Name)] = e.Location
	}
	for _, e := range flow.Edges {
		from, to := id(e.From), id(e.To)
		v.Edges = append(v.Edges, Edge{From: from, To: to, CrossesNetwork: location[from] != location[to]})
	}
	return v, nil
}

// MarkBottlenecks marks, on every path from a producer to the end of the
// flow, the engine that is busiest, if it is busy at least
// BottleneckBusyFraction of the time or skips at least BottleneckSkippedShare
// of its inputs. Busy time, not latency,
// decides: a slow engine that is rarely fed holds nothing up.
func MarkBottlenecks(v *View) {
	index := make(map[string]int, len(v.Nodes))
	for i, n := range v.Nodes {
		index[n.ID] = i
	}
	next := map[string][]string{}
	for _, e := range v.Edges {
		next[e.From] = append(next[e.From], e.To)
	}
	var walk func(id string, busiest int)
	walk = func(id string, busiest int) {
		if i, ok := index[id]; ok && busyOf(v.Nodes[i]) > busyOf(nodeAt(v, busiest)) {
			busiest = i
		}
		if len(next[id]) == 0 {
			if busiest >= 0 && holdsUp(v.Nodes[busiest]) {
				v.Nodes[busiest].Bottleneck = true
			}
			return
		}
		for _, to := range next[id] {
			walk(to, busiest)
		}
	}
	for _, n := range v.Nodes {
		if n.Kind == KindProducer {
			walk(n.ID, -1)
		}
	}
}

// noBusy ranks below any measured busy fraction.
const noBusy = -1

func nodeAt(v *View, i int) Node {
	if i < 0 {
		return Node{}
	}
	return v.Nodes[i]
}

func busyOf(n Node) float64 {
	if n.Kind != KindEngine || n.Metrics == nil || n.Metrics.Busy == nil {
		return noBusy
	}
	return *n.Metrics.Busy
}

func holdsUp(n Node) bool {
	if busyOf(n) == noBusy {
		return false
	}
	skips := n.Metrics.Skipped != nil && *n.Metrics.Skipped >= BottleneckSkippedShare
	return *n.Metrics.Busy >= BottleneckBusyFraction || skips
}
