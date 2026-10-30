package main

import (
	"errors"
	"math"
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

// mockObjects are what the mock detector finds in every frame. They stay
// inside the frame, and their scores rise and fall over time.
var mockObjects = []mockObject{
	{class: "person", cx: 0.30, cy: 0.55, rx: 0.15, ry: 0.15, w: 0.08, h: 0.25, period: 8 * time.Second},
	{class: "car", cx: 0.65, cy: 0.40, rx: 0.20, ry: 0.10, w: 0.20, h: 0.14, period: 12 * time.Second, phase: 0.3},
	{class: "dog", cx: 0.50, cy: 0.75, rx: 0.30, ry: 0.08, w: 0.10, h: 0.10, period: 6 * time.Second, phase: 0.6},
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

// Detector is the handler of the mock detector: it forwards every frame, with
// the mock objects as its result.
func Detector(eng *uriel.Engine, in uriel.Input) error {
	result, err := proto.Marshal(MockDetections(feed.VehicleOf(in.ID.Producer), time.Now()))
	if err != nil {
		return errors.Join(err, eng.Forward(nil))
	}
	return eng.Forward(result)
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
