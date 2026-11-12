package main

import (
	"errors"
	"math"
	"math/rand/v2"
	"slices"
	"strings"
	"time"

	resultpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/result"
	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/feed"
	"github.com/cmusatyalab/uriel/go"
	"google.golang.org/protobuf/proto"
)

const (
	// detectorService is the bare name of the mock detector, which runs on
	// the GCS's own daemon so the GCS can show its results.
	detectorService = "mock_detector"
	// detectorMaxOutput is the output size of the detector, which only
	// forwards.
	detectorMaxOutput = 4096
	// detectorTimeout is the timeout of the edges to the detector.
	detectorTimeout = "1s"
)

// mockObject is an object the mock detector finds: it moves around an ellipse
// centered at (cx, cy), with radii (rx, ry), once a period, starting at phase
// (in turns).
type mockObject struct {
	class          string
	cx, cy, rx, ry float64
	w, h           float64
	period         time.Duration
	phase          float64
}

// mockObjects are what the mock detector finds in every frame, wherever the
// camera looks: they stay inside the frame and wander, so on the map they
// stand for an object that moves. Their scores rise and fall over time.
var mockObjects = []mockObject{
	{class: "dog", cx: 0.50, cy: 0.75, rx: 0.30, ry: 0.08, w: 0.10, h: 0.10, period: 6 * time.Second, phase: 0.6},
}

// worldObject is an object fixed on the ground, north and east metres from
// each vehicle's home, which the mock detector finds while the vehicle's
// camera looks at it. Its box is w by h of the frame.
type worldObject struct {
	class       string
	north, east float64
	w, h        float64
}

// worldObjects are in front of a vehicle that takes off from home facing
// north and tilts its gimbal down by 30° to 45°.
var worldObjects = []worldObject{
	{class: "person", north: 25, east: 0, w: 0.06, h: 0.10},
	{class: "car", north: 40, east: 10, w: 0.06, h: 0.10},
}

const (
	// mockHFov and mockVFov are the mock camera's field of view, in degrees.
	mockHFov, mockVFov = 90, 60
	// worldJitter is how far, as a fraction of the frame, a world object's
	// box moves at random from frame to frame.
	worldJitter = 0.005
)

// worldDetections returns the world objects v's camera sees, each with a box
// around where it is in the frame: the inverse of placing a box on the
// ground (pins.Project), with the gimbal's pitch and the vehicle's heading.
// A vehicle on the ground sees none.
func worldDetections(v *Vehicle) []*resultpb.Detection {
	pos, alt, yaw, pitch, flying := v.CameraPose()
	if !flying {
		return nil
	}
	home := v.Home()
	rad := math.Pi / 180
	p, y := pitch*rad, yaw*rad
	th, tv := math.Tan(mockHFov/2*rad), math.Tan(mockVFov/2*rad)
	var out []*resultpb.Detection
	for _, o := range worldObjects {
		n0, e0 := metersBetween(pos, home)
		n, e := n0+o.north, e0+o.east
		fwd := n*math.Cos(y) + e*math.Sin(y)
		right := -n*math.Sin(y) + e*math.Cos(y)
		// The object in the camera's frame: along its optical axis, and up
		// in its image.
		depth := fwd*math.Cos(p) - alt*math.Sin(p)
		up := -fwd*math.Sin(p) - alt*math.Cos(p)
		if depth <= 0 {
			continue
		}
		cx := (right/depth/th+1)/2 + worldJitter*(2*rand.Float64()-1)
		cy := (1-up/depth/tv)/2 + worldJitter*(2*rand.Float64()-1)
		if cx < 0 || cx > 1 || cy < 0 || cy > 1 {
			continue
		}
		out = append(out, resultpb.Detection_builder{
			ClassName: o.class,
			Score:     0.8,
			Bbox: resultpb.BoundingBox_builder{
				XMin: math.Max(0, cx-o.w/2), YMin: math.Max(0, cy-o.h/2),
				XMax: math.Min(1, cx+o.w/2), YMax: math.Min(1, cy+o.h/2),
			}.Build(),
		}.Build())
	}
	return out
}

// MockDetections returns what the mock detector finds in a frame of vehicle
// at t: every mock object, where it is at t. Each vehicle's objects are
// offset in time, so the vehicles' feeds differ.
func MockDetections(vehicle string, t time.Time) *resultpb.DetectionResult {
	var offset float64
	for _, c := range vehicle {
		offset += float64(c)
	}
	detections := make([]*resultpb.Detection, 0, len(mockObjects))
	for _, o := range mockObjects {
		turn := float64(t.UnixNano())/float64(o.period) + o.phase + offset/10
		angle := 2 * math.Pi * turn
		x, y := o.cx+o.rx*math.Cos(angle), o.cy+o.ry*math.Sin(angle)
		detections = append(detections, resultpb.Detection_builder{
			ClassName: o.class,
			Score:     0.75 + 0.2*math.Sin(angle/2),
			Bbox: resultpb.BoundingBox_builder{
				XMin: x - o.w/2, YMin: y - o.h/2, XMax: x + o.w/2, YMax: y + o.h/2,
			}.Build(),
		}.Build())
	}
	return resultpb.DetectionResult_builder{Detections: detections}.Build()
}

// NewDetector returns the handler of the mock detector: it forwards every
// frame, with the mock objects and the world objects the frame's vehicle
// (from vehicles, by name) sees as its result.
func NewDetector(vehicles map[string]*Vehicle) EngineFunc {
	return func(eng *uriel.Engine, in uriel.Input) error {
		name := feed.VehicleOf(in.ID.Producer)
		det := MockDetections(name, time.Now())
		if v := vehicles[name]; v != nil {
			det = resultpb.DetectionResult_builder{Detections: append(det.GetDetections(), worldDetections(v)...)}.Build()
		}
		result, err := proto.Marshal(det)
		if err != nil {
			return errors.Join(err, eng.Forward(nil))
		}
		return eng.Forward(result)
	}
}

// withDetector returns flow with an edge from each of its cameras to the
// detector, so the detector sees every frame the vehicle sends. Producers are
// qualified; every producer but the telemetry is a camera.
func withDetector(flow *config.FlowConfig, detector string) *config.FlowConfig {
	var cameras []string
	for _, p := range flow.Producers {
		if !strings.HasSuffix(p.Name, ".telemetry") {
			cameras = append(cameras, p.Name)
		}
	}
	if len(cameras) == 0 {
		return flow
	}
	if !slices.ContainsFunc(flow.Engines, func(e config.Engine) bool { return e.Name == detector }) {
		flow.Engines = append(flow.Engines, config.Engine{Name: detector, Location: config.LocationLocal})
	}
	for _, cam := range cameras {
		edge := config.Edge{From: cam, To: detector, Timeout: detectorTimeout}
		if !slices.Contains(flow.Edges, edge) {
			flow.Edges = append(flow.Edges, edge)
		}
	}
	return flow
}
