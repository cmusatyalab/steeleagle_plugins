package fleet

import (
	"errors"
	"reflect"
	"testing"

	"github.com/cmusatyalab/steeleagle/config"
)

// checkFlow fails the test unless the flow is valid and has exactly the given
// producers, engines and edges.
func checkFlow(t *testing.T, flow *config.FlowConfig, p []config.Producer, e []config.Engine, ed []config.Edge) {
	t.Helper()
	if err := flow.Validate(); err != nil {
		t.Fatalf("flow invalid: %v", err)
	}
	if !reflect.DeepEqual(flow.Producers, p) {
		t.Errorf("producers = %v, want %v", flow.Producers, p)
	}
	if !reflect.DeepEqual(flow.Engines, e) {
		t.Errorf("engines = %v, want %v", flow.Engines, e)
	}
	if !reflect.DeepEqual(flow.Edges, ed) {
		t.Errorf("edges = %v, want %v", flow.Edges, ed)
	}
}

func driverWith(cams ...config.DriverCamera) *config.DriverCap {
	return &config.DriverCap{Cameras: cams}
}

func TestBuildFlowLocal(t *testing.T) {
	v := Vehicle{Name: "100.64.1.5.cassowary", DaemonIP: "100.64.1.5",
		Driver: driverWith(config.DriverCamera{Name: "front", Encode: true})}
	snap := Snapshot{Encoders: map[string]string{"100.64.1.5": "100.64.1.5.encoder"}}
	flow, noVideo := BuildFlow(v, snap, FlowSpec{Engine: "100.64.1.5.gcs"})
	if noVideo != "" {
		t.Errorf("noVideo = %q, want empty", noVideo)
	}
	checkFlow(t, flow, []config.Producer{{Name: "telemetry"}, {Name: "front"}}, []config.Engine{
		{Name: "100.64.1.5.encoder", Location: config.LocationLocal},
		{Name: "100.64.1.5.gcs", Location: config.LocationLocal},
	}, []config.Edge{
		{From: "telemetry", To: "100.64.1.5.gcs", Timeout: "2s"},
		{From: "front", To: "100.64.1.5.encoder", Timeout: "2s"},
		{From: "100.64.1.5.encoder", To: "100.64.1.5.gcs", Timeout: "2s"},
	})
}

func TestBuildFlowRemote(t *testing.T) {
	v := Vehicle{Name: "100.64.2.9.harpy", DaemonIP: "100.64.2.9",
		Driver: driverWith(config.DriverCamera{Name: "front", Encode: true})}
	snap := Snapshot{Encoders: map[string]string{"100.64.2.9": "100.64.2.9.encoder"}}
	flow, noVideo := BuildFlow(v, snap, FlowSpec{Engine: "100.64.1.5.gcs"})
	if noVideo != "" {
		t.Errorf("noVideo = %q, want empty", noVideo)
	}
	checkFlow(t, flow, []config.Producer{{Name: "telemetry"}, {Name: "front"}}, []config.Engine{
		{Name: "100.64.2.9.encoder", Location: config.LocationLocal},
		{Name: "100.64.1.5.gcs", Location: config.LocationRemote},
	}, []config.Edge{
		{From: "telemetry", To: "100.64.1.5.gcs", Timeout: "2s"},
		{From: "front", To: "100.64.2.9.encoder", Timeout: "2s"},
		{From: "100.64.2.9.encoder", To: "100.64.1.5.gcs", Timeout: "2s"},
	})
}

func TestBuildFlowNoVideo(t *testing.T) {
	const gcs = "100.64.1.5.gcs"
	enc := map[string]string{"100.64.2.9": "100.64.2.9.encoder"}
	tests := []struct {
		name     string
		driver   *config.DriverCap
		err      error
		encoders map[string]string
		want     string
		noIP     bool // the vehicle has no daemon IP
	}{
		{"driver error", nil, errors.New("boom"), enc, "driver config unavailable: boom", false},
		{"no cameras", driverWith(), nil, enc, "vehicle has no cameras", false},
		{"first camera not encodable", driverWith(
			config.DriverCamera{Name: "front", Encode: false},
			config.DriverCamera{Name: "rear", Encode: true}), nil, enc, "camera front cannot be encoded", false},
		{"encoder only on gcs daemon", driverWith(config.DriverCamera{Name: "front", Encode: true}), nil,
			map[string]string{"100.64.1.5": "100.64.1.5.encoder"},
			"no encoder plugin of the h264 package on the vehicle's daemon 100.64.2.9", false},
		{"no daemon address", driverWith(config.DriverCamera{Name: "front", Encode: true}), nil,
			map[string]string{"": "encoder"},
			"no encoder: vehicle has no daemon address", true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			v := Vehicle{Name: "100.64.2.9.harpy", DaemonIP: "100.64.2.9", Driver: tt.driver, DriverErr: tt.err}
			if tt.noIP {
				v.Name, v.DaemonIP = "harpy", ""
			}
			flow, noVideo := BuildFlow(v, Snapshot{Encoders: tt.encoders}, FlowSpec{Engine: gcs})
			if noVideo != tt.want {
				t.Errorf("noVideo = %q, want %q", noVideo, tt.want)
			}
			checkFlow(t, flow, []config.Producer{{Name: "telemetry"}},
				[]config.Engine{{Name: gcs, Location: config.LocationRemote}},
				[]config.Edge{{From: "telemetry", To: gcs, Timeout: "2s"}})
		})
	}
}
