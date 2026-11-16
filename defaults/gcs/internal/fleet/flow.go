package fleet

import (
	"fmt"

	"github.com/cmusatyalab/steeleagle/config"
)

// EncoderPlugin and EncoderPackage identify the engine that encodes a
// vehicle's camera frames before they are sent to the GCS: the plugin
// encoder.plugin.toml of the h264 package. Its service may have any name.
const (
	EncoderPlugin  = "encoder.plugin.toml"
	EncoderPackage = "h264"
)

// edgeTimeout is how long every edge of the flow waits on its target.
const edgeTimeout = "2s"

// cameraTokens is how many of the camera's frames may be in the flow at once.
// It is set explicitly: left unset, Uriel computes one per engine, edge and
// spare plus the remote crossing's capacity (8 for this flow).
const cameraTokens = 1

// FlowSpec is what a flow depends on beyond the vehicle.
type FlowSpec struct {
	// Engine is the GCS engine's full service name.
	Engine string
}

// BuildFlow builds the dataflow that carries a vehicle's telemetry, and its
// video if it can, to the GCS engine. Nodes have the names a flow gives them
// relative to the vehicle (see flownames.go), which it qualifies itself.
// noVideo is empty if the flow carries video, and otherwise says why it does
// not.
func BuildFlow(v Vehicle, snap Snapshot, spec FlowSpec) (flow *config.FlowConfig, noVideo string) {
	gcs, gcsLoc := FlowEngine(spec.Engine, v.DaemonIP)
	telemetry := VehicleStream("telemetry")
	flow = &config.FlowConfig{
		Producers: []config.Producer{{Name: telemetry}},
		Engines:   []config.Engine{{Name: gcs, Location: gcsLoc}},
		Edges:     []config.Edge{{From: telemetry, To: gcs, Timeout: edgeTimeout}},
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

	cam := VehicleStream(v.Driver.Cameras[0].Name)
	enc, _ := FlowEngine(encoder, v.DaemonIP) // on the vehicle's daemon
	tokens := cameraTokens
	flow.Producers = append(flow.Producers, config.Producer{Name: cam, Tokens: &tokens})
	flow.Engines = []config.Engine{
		{Name: enc, Location: config.LocationLocal},
		flow.Engines[0],
	}
	flow.Edges = append(flow.Edges,
		config.Edge{From: cam, To: enc, Timeout: edgeTimeout},
		config.Edge{From: enc, To: gcs, Timeout: edgeTimeout})
	return flow, ""
}
