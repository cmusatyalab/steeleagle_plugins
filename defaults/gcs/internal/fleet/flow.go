package fleet

import (
	"fmt"

	"github.com/cmusatyalab/steeleagle/config"
)

// EncoderPlugin and EncoderPackage identify the engine that encodes a
// vehicle's camera frames before they are sent to the GCS: the plugin
// encoder.plugin.toml of the h264 package. Its service may have any name.
const (
	EncoderPlugin  = "encoder"
	EncoderPackage = "h264"
)

// edgeTimeout is how long every edge of the flow waits on its target.
const edgeTimeout = "2s"

// FlowSpec is what a flow depends on beyond the vehicle.
type FlowSpec struct {
	// Engine is the GCS engine's full service name.
	Engine string
}

// BuildFlow builds the dataflow that carries a vehicle's telemetry, and its
// video if it can, to the GCS engine. Producer names are the short ones the
// vehicle qualifies itself; engine names are full service names. noVideo is
// empty if the flow carries video, and otherwise says why it does not.
func BuildFlow(v Vehicle, snap Snapshot, spec FlowSpec) (flow *config.FlowConfig, noVideo string) {
	gcsIP, _ := SplitName(spec.Engine)
	gcsLoc := config.LocationRemote
	if gcsIP == v.DaemonIP {
		gcsLoc = config.LocationLocal
	}
	flow = &config.FlowConfig{
		Producers: []config.Producer{{Name: "telemetry"}},
		Engines:   []config.Engine{{Name: spec.Engine, Location: gcsLoc}},
		Edges:     []config.Edge{{From: "telemetry", To: spec.Engine, Timeout: edgeTimeout}},
	}

	encoder, hasEncoder := snap.Encoders[v.DaemonIP]
	switch {
	case v.DriverErr != nil:
		return flow, fmt.Sprintf("driver config unavailable: %v", v.DriverErr)
	case v.Driver == nil || len(v.Driver.Cameras) == 0:
		return flow, "vehicle has no cameras"
	case !v.Driver.Cameras[0].Encode:
		return flow, fmt.Sprintf("camera %s cannot be encoded", v.Driver.Cameras[0].Name)
	case v.DaemonIP == "":
		return flow, "no encoder: vehicle has no daemon address"
	case !hasEncoder:
		return flow, fmt.Sprintf("no %s plugin of the %s package on the vehicle's daemon %s",
			EncoderPlugin, EncoderPackage, v.DaemonIP)
	}

	cam := v.Driver.Cameras[0].Name
	flow.Producers = append(flow.Producers, config.Producer{Name: cam})
	flow.Engines = []config.Engine{
		{Name: encoder, Location: config.LocationLocal},
		flow.Engines[0],
	}
	flow.Edges = append(flow.Edges,
		config.Edge{From: cam, To: encoder, Timeout: edgeTimeout},
		config.Edge{From: encoder, To: spec.Engine, Timeout: edgeTimeout})
	return flow, ""
}
