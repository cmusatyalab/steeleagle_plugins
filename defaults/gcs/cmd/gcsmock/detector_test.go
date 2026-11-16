package main

import (
	"math"
	"testing"
	"time"

	resultpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/result"
	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/pins"
)

// TestMockDetections ensures that the mock objects stay inside the frame and
// move over time.
func TestMockDetections(t *testing.T) {
	start := time.Unix(1_700_000_000, 0)
	first := MockDetections("100.64.0.1.alpha", start)
	if len(first.GetDetections()) != len(mockObjects) {
		t.Fatalf("got %d detections, want %d", len(first.GetDetections()), len(mockObjects))
	}
	for step := range 100 {
		at := start.Add(time.Duration(step) * 137 * time.Millisecond)
		for _, d := range MockDetections("100.64.0.1.alpha", at).GetDetections() {
			b := d.GetBbox()
			if b.GetXMin() < 0 || b.GetYMin() < 0 || b.GetXMax() > 1 || b.GetYMax() > 1 ||
				b.GetXMin() >= b.GetXMax() || b.GetYMin() >= b.GetYMax() {
				t.Fatalf("at %v, %s has box %v outside the frame", at, d.GetClassName(), b)
			}
			if d.GetScore() <= 0 || d.GetScore() > 1 {
				t.Fatalf("at %v, %s has score %v", at, d.GetClassName(), d.GetScore())
			}
		}
	}
	later := MockDetections("100.64.0.1.alpha", start.Add(time.Second))
	if later.GetDetections()[0].GetBbox().GetXMin() == first.GetDetections()[0].GetBbox().GetXMin() {
		t.Error("the objects don't move")
	}
	other := MockDetections("100.64.0.1.bravo", start)
	if other.GetDetections()[0].GetBbox().GetXMin() == first.GetDetections()[0].GetBbox().GetXMin() {
		t.Error("two vehicles see the same objects")
	}
}

// TestWithDetector ensures that every camera of a flow, but not its
// telemetry, gets an edge to the detector, once.
func TestWithDetector(t *testing.T) {
	const detector = "100.64.0.1.mock_detector"
	flow := &config.FlowConfig{
		Producers: []config.Producer{{Name: "100.64.0.1.alpha.front"}, {Name: "100.64.0.1.alpha.telemetry"}},
		Engines:   []config.Engine{{Name: "100.64.0.1.h264_encoder", Location: config.LocationLocal}},
		Edges:     []config.Edge{{From: "100.64.0.1.alpha.front", To: "100.64.0.1.h264_encoder", Timeout: "2s"}},
	}
	got := withDetector(withDetector(flow, detector), detector)
	if len(got.Engines) != 2 || got.Engines[1] != (config.Engine{Name: detector, Location: config.LocationLocal}) {
		t.Errorf("engines = %+v, want the encoder and the detector", got.Engines)
	}
	want := config.Edge{From: "100.64.0.1.alpha.front", To: detector, Timeout: detectorTimeout}
	if len(got.Edges) != 2 || got.Edges[1] != want {
		t.Errorf("edges = %+v, want the encoder's and %+v", got.Edges, want)
	}

	telemetryOnly := &config.FlowConfig{Producers: []config.Producer{{Name: "100.64.0.1.alpha.telemetry"}}}
	if got := withDetector(telemetryOnly, detector); len(got.Engines) != 0 || len(got.Edges) != 0 {
		t.Errorf("a flow without cameras got %+v", got)
	}
}

// flyingAt returns a vehicle at its home, flying at 20 m, facing heading with
// the gimbal at pitch.
func flyingAt(heading, pitch float64) *Vehicle {
	v := NewVehicle("100.64.0.1.alpha", LatLon{Lat: 40.4433, Lon: -79.9436})
	v.mode, v.alt, v.heading, v.gimbalPitch = Flying, 20, heading, pitch
	return v
}

func classes(ds []*resultpb.Detection) map[string]*resultpb.Detection {
	out := map[string]*resultpb.Detection{}
	for _, d := range ds {
		out[d.GetClassName()] = d
	}
	return out
}

// TestWorldDetectionsVisibleWhenLookingAtThem ensures that a box of an
// object fixed on the ground projects back onto it.
func TestWorldDetectionsVisibleWhenLookingAtThem(t *testing.T) {
	car := worldObjects[1]
	heading := math.Atan2(car.east, car.north) * 180 / math.Pi
	v := flyingAt(heading, -35)
	d := classes(worldDetections(v))["car"]
	if d == nil {
		t.Fatal("no car seen while looking at it")
	}
	b := d.GetBbox()
	pose := pins.CameraPose{Lat: v.home.Lat, Lon: v.home.Lon, Alt: 20, Yaw: heading, Pitch: -35}
	lat, lon, ok := pins.Project(pose, pins.FOV{H: mockHFov, V: mockVFov}, (b.GetXMin()+b.GetXMax())/2, (b.GetYMin()+b.GetYMax())/2)
	if !ok {
		t.Fatal("the car's box doesn't project to the ground")
	}
	n, e := metersBetween(v.home, LatLon{lat, lon})
	if math.Hypot(n-car.north, e-car.east) > 1.5 {
		t.Fatalf("car's box projects to %.1f m north, %.1f m east; want %.0f, %.0f", n, e, car.north, car.east)
	}
}

func TestWorldDetectionsNoneWhenLanded(t *testing.T) {
	v := flyingAt(0, -35)
	v.mode, v.alt = Landed, 0
	if got := worldDetections(v); len(got) != 0 {
		t.Fatalf("landed vehicle sees %d objects", len(got))
	}
}

func TestWorldDetectionsOutsideFrameOmitted(t *testing.T) {
	if got := worldDetections(flyingAt(180, -35)); len(got) != 0 {
		t.Fatalf("facing away, saw %v", classes(got))
	}
}
