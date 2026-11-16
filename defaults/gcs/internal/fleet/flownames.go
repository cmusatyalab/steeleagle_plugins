package fleet

import (
	"strings"

	"github.com/cmusatyalab/steeleagle/config"
)

// A flow names its nodes relative to the vehicle that runs it, which
// qualifies them itself:
//   - vehicle.<stream>: the vehicle's telemetry, or one of its cameras;
//   - local.<engine>.<producer>: a producer of an engine on the vehicle's
//     daemon (an engine on the orchestrator can't feed a flow a producer);
//   - local.<engine> and remote.<engine>: an engine on the vehicle's daemon or
//     on the orchestrator. Missions name engines the same way.
const (
	VehiclePrefix = "vehicle."
	LocalPrefix   = config.LocationLocal + "."
	RemotePrefix  = config.LocationRemote + "."
)

// VehicleStream is the flow name of the vehicle's stream (telemetry or a
// camera).
func VehicleStream(stream string) string {
	return VehiclePrefix + stream
}

// FlowEngine returns the flow name of the engine with the full service name
// full, and its location, for a vehicle on the daemon vehicleIP: local if the
// engine is on that daemon, remote otherwise.
func FlowEngine(full, vehicleIP string) (name, location string) {
	ip, bare := SplitName(full)
	if ip != "" && ip == vehicleIP {
		return LocalPrefix + bare, config.LocationLocal
	}
	return RemotePrefix + bare, config.LocationRemote
}

// LocalEngineProducer is the flow name of the producer called producer of the
// engine bare on the vehicle's daemon.
func LocalEngineProducer(bare, producer string) string {
	return LocalPrefix + bare + "." + producer
}

// QualifyFlowName returns the name Uriel knows the flow node name by, as the
// vehicle (full name vehicle, on the daemon vehicleIP) qualifies it with an
// orchestrator at orchestratorIP: a vehicle stream with the vehicle's name,
// and an engine or engine producer with its daemon's IP. A name without a
// known prefix is returned as it is.
func QualifyFlowName(name, vehicle, vehicleIP, orchestratorIP string) string {
	if stream, ok := strings.CutPrefix(name, VehiclePrefix); ok {
		return vehicle + "." + stream
	}
	if rest, ok := strings.CutPrefix(name, LocalPrefix); ok {
		return vehicleIP + "." + rest
	}
	if rest, ok := strings.CutPrefix(name, RemotePrefix); ok {
		return orchestratorIP + "." + rest
	}
	return name
}
