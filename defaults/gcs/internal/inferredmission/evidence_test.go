package inferredmission

import (
	"slices"
	"testing"
	"time"

	commonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/common"
	telemetrypb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/telemetry"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/feed"
)

func TestLiveEvidenceUsesExactTransportIdentity(t *testing.T) {
	at := time.Now()
	cache := evidenceCache{engine: "10.0.0.1.detector"}
	detection := func(session, seq uint64, engine, label string) feed.Result {
		return feed.Result{Engine: engine, Session: session, Seq: seq, Type: "DetectionResult",
			JSON: []byte(`{"detections":[{"className":"` + label + `","score":0.91}]}`)}
	}
	cache.addResult(detection(1, 7, cache.engine, "wrong session"))
	cache.addResult(detection(2, 8, cache.engine, "unsampled"))
	cache.addResult(detection(2, 7, cache.engine, "Dog"))
	cache.addResult(detection(2, 9, "10.0.0.1.other", "wrong engine"))
	cache.addResult(feed.Result{Engine: cache.engine, Session: 2, Seq: 10, Type: "DetectionResult",
		JSON: []byte(`{"detections":[]}`)})

	cache.addMotion(timedMotion{at: at.Add(-4 * time.Second), source: "telemetry",
		motion: MotionEvidence{RelativeAltitude: new(80.0)}})
	cache.addMotion(timedMotion{at: at, source: "telemetry",
		motion: MotionEvidence{RelativeAltitude: new(5.0), Motion: "HOLDING"}})
	cache.addMotion(timedMotion{at: at.Add(time.Second), source: "receipt",
		motion: MotionEvidence{RelativeAltitude: new(6.0)}})

	fresh := cache.nearby(at.Add(100 * time.Millisecond))
	if fresh == nil || fresh.Source != "telemetry" || fresh.OffsetSec != -0.1 ||
		fresh.RelativeAltitude == nil || *fresh.RelativeAltitude != 5 {
		t.Fatalf("expected nearest fresh telemetry, got %+v", fresh)
	}
	if stale := cache.nearby(at.Add(4 * time.Second)); stale != nil {
		t.Fatalf("stale telemetry was attached: %+v", stale)
	}

	job := liveSection{origin: at, indices: []int{1, 2, 3, 4}, packets: []livePacket{
		{at: at, identity: frameIdentity{2, 8}}, // decoded but not selected
		{at: at.Add(time.Second), id: 9, identity: frameIdentity{2, 7}, telemetry: fresh},
		{at: at.Add(2 * time.Second), id: 7, identity: frameIdentity{2, 11}},
		{at: at.Add(3 * time.Second), identity: frameIdentity{2, 9}},
		{at: at.Add(4 * time.Second), identity: frameIdentity{2, 10}},
	}}
	context := cache.snapshot("10.0.0.1.alpha.front", job)
	if context.SampledFrames != 4 || context.DetectionFrames != 1 || context.MotionFrames != 1 ||
		len(context.Frames) != 1 || context.Frames[0].Frame != 1 || context.Frames[0].Time != 1 {
		t.Fatalf("expected one frame with matched evidence, got %+v", context)
	}
	if got := context.Frames[0].Detections; len(got) != 1 || got[0].Class != "Dog" {
		t.Fatalf("expected exact native detector label, got %+v", got)
	}
}

func TestLiveEvidenceNumbersTheBestBoxesWithTheirPositions(t *testing.T) {
	cache := evidenceCache{engine: "10.0.0.1.detector"}
	cache.addResult(feed.Result{
		Engine: cache.engine, Session: 2, Seq: 7, Type: "DetectionResult",
		JSON: []byte(`{"detections":[
			{"className":"Person","score":0.90,"bbox":{"xMin":0.1,"yMin":0.2,"xMax":0.3,"yMax":0.6}},
			{"className":"Person","score":0.99,"bbox":{"xMin":0.4,"yMin":0.4,"xMax":0.5,"yMax":0.7}},
			{"className":"Person","score":0.91},
			{"className":"Person","score":0.95},
			{"className":"Dog","score":0.87},
			{"className":"dog","score":0.86},
			{"className":"Box","score":0.86,"bbox":{"xMin":0.5,"yMin":0.5,"xMax":0.4,"yMax":0.6}},
			{"className":"Cone","score":0.8},
			{"className":"Cone","score":0.7}
		]}`),
	})
	box := func(b ...float64) *[4]float64 { return (*[4]float64)(b) }
	// Several boxes of one class are all kept: any may be the one tracked
	expected := []detectorLabel{
		{ID: 1, Class: "Person", Score: 0.99, Box: box(0.4, 0.4, 0.5, 0.7)},
		{ID: 2, Class: "Person", Score: 0.95},
		{ID: 3, Class: "Person", Score: 0.91},
		{ID: 4, Class: "Person", Score: 0.9, Box: box(0.1, 0.2, 0.3, 0.6)},
		{ID: 5, Class: "Dog", Score: 0.87},
		{ID: 6, Class: "Box", Score: 0.86}, // an inverted box is dropped
		{ID: 7, Class: "dog", Score: 0.86},
		{ID: 8, Class: "Cone", Score: 0.8},
	}
	labels := cache.results[frameIdentity{2, 7}]
	if !slices.EqualFunc(labels, expected, func(a, b detectorLabel) bool {
		return a.ID == b.ID && a.Class == b.Class && a.Score == b.Score && (a.Box == nil) == (b.Box == nil) && (a.Box == nil || *a.Box == *b.Box)
	}) {
		t.Fatalf("expected the best %d boxes, numbered, got %+v", maxDetectionLabels, labels)
	}
}

func TestCameraMotionKeepsMeasuredZeroAndOmitsUnavailableFields(t *testing.T) {
	position := telemetrypb.PositionInfo_builder{
		RelativePosition: commonpb.RelativePosition_builder{Z: new(float32(0))}.Build(),
		VelocityBody:     commonpb.Velocity_builder{XVel: new(float32(0)), ZVel: new(float32(0))}.Build(),
	}.Build()
	motion := CameraMotion(position, nil)
	if motion.RelativeAltitude == nil || *motion.RelativeAltitude != 0 ||
		motion.VerticalSpeed == nil || *motion.VerticalSpeed != 0 {
		t.Fatalf("expected measured zero altitude and speed, got %+v", motion)
	}
	if motion.HorizontalSpeed != nil || motion.GimbalPitch != nil || CameraMotion(nil, nil) != (MotionEvidence{}) {
		t.Fatalf("an unavailable measurement was assigned a value: %+v", motion)
	}
}
