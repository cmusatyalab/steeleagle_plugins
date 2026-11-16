package fleet

import "testing"

func TestFlowEngine(t *testing.T) {
	for _, tc := range []struct {
		full, vehicleIP, name, location string
	}{
		{"100.64.0.2.yolo", "100.64.0.2", "local.yolo", "local"},
		{"100.64.0.1.gcs", "100.64.0.2", "remote.gcs", "remote"},
		// Without the vehicle's daemon, every engine is remote
		{"100.64.0.2.yolo", "", "remote.yolo", "remote"},
	} {
		name, location := FlowEngine(tc.full, tc.vehicleIP)
		if name != tc.name || location != tc.location {
			t.Errorf("FlowEngine(%q, %q) = %q, %q, want %q, %q", tc.full, tc.vehicleIP, name, location, tc.name, tc.location)
		}
	}
	if got := VehicleStream("front"); got != "vehicle.front" {
		t.Errorf("VehicleStream = %q", got)
	}
	if got := LocalEngineProducer("enc", "frames"); got != "local.enc.frames" {
		t.Errorf("LocalEngineProducer = %q", got)
	}
}

// QualifyFlowName names each node as the vehicle qualifies it.
func TestQualifyFlowName(t *testing.T) {
	const vehicle, vehicleIP, orchestrator = "100.64.0.2.harpy", "100.64.0.2", "100.64.0.1"
	for name, want := range map[string]string{
		"vehicle.front":     "100.64.0.2.harpy.front",
		"vehicle.telemetry": "100.64.0.2.harpy.telemetry",
		"local.yolo":        "100.64.0.2.yolo",
		"local.enc.frames":  "100.64.0.2.enc.frames",
		"remote.gcs":        "100.64.0.1.gcs",
		"unprefixed":        "unprefixed",
	} {
		if got := QualifyFlowName(name, vehicle, vehicleIP, orchestrator); got != want {
			t.Errorf("QualifyFlowName(%q) = %q, want %q", name, got, want)
		}
	}
}
