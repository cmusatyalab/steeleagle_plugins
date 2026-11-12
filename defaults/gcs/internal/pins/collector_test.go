package pins

import (
	"context"
	"sync"
	"testing"
	"time"

	resultpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/result"
	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/feed"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/flows"
	"github.com/rs/zerolog"
	"google.golang.org/protobuf/encoding/protojson"
)

func detectionJSON(t *testing.T, cx, cy float64) []byte {
	t.Helper()
	b, err := protojson.Marshal(resultpb.DetectionResult_builder{Detections: []*resultpb.Detection{
		resultpb.Detection_builder{ClassName: "car", Score: 0.9, Bbox: resultpb.BoundingBox_builder{
			XMin: cx - 0.05, YMin: cy - 0.05, XMax: cx + 0.05, YMax: cy + 0.05,
		}.Build()}.Build(),
	}}.Build())
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func result(t *testing.T, producer string, seq uint64, cx, cy float64) feed.Result {
	return feed.Result{
		Engine: "det", Producer: producer, Vehicle: feed.VehicleOf(producer), Seq: seq,
		Type: "DetectionResult", JSON: detectionJSON(t, cx, cy), OfferedAt: base,
	}
}

// collector returns a collector of alpha, whose cameras are cams, with its
// telemetry looking north-east and down at 45° from 20 m.
func collector(cams ...config.DriverCamera) (*Collector, *Store) {
	store := NewStore()
	poses := NewPoseHistory()
	poses.Telemetry("alpha", telemetry(testLat, testLon, 20, 45, -45), base)
	c := NewCollector(store, poses, zerolog.Nop())
	c.SetVehicles([]fleet.Vehicle{{Name: "alpha", Driver: &config.DriverCap{Cameras: cams}}})
	return c, store
}

var rgb = config.DriverCamera{Name: "rgb", Gimbal: true, HFov: 69, VFov: 42}

func TestHandleProjectsGimbalCamera(t *testing.T) {
	c, store := collector(rgb)
	for seq := uint64(1); seq <= 3; seq++ {
		c.Handle(result(t, "alpha.rgb", seq, 0.5, 0.5))
	}
	got := store.Snapshot(base)
	if len(got) != 1 || got[0].Class != "car" || got[0].Engine != "det" {
		t.Fatalf("got %+v, want one car pin", got)
	}
	n, e := metres(testLat, testLon, got[0].Lat, got[0].Long)
	if d := n*n + e*e; d < 19*19 || d > 21*21 || n < 0 || e < 0 {
		t.Fatalf("pin %.1f m north, %.1f m east; want ~20 m north-east", n, e)
	}
}

func TestHandleDropsNonGimbalCamera(t *testing.T) {
	c, store := collector(config.DriverCamera{Name: "down", HFov: 69, VFov: 42})
	for seq := uint64(1); seq <= 3; seq++ {
		c.Handle(result(t, "alpha.down", seq, 0.5, 0.5))
	}
	if got := store.Snapshot(base); len(got) != 0 {
		t.Fatalf("got %d pins from a fixed camera, want 0", len(got))
	}
}

func TestHandleDropsUnknownCamera(t *testing.T) {
	c, store := collector(rgb)
	for seq := uint64(1); seq <= 3; seq++ {
		c.Handle(result(t, "alpha.ir", seq, 0.5, 0.5))
	}
	if got := store.Snapshot(base); len(got) != 0 {
		t.Fatalf("got %d pins from an unknown camera, want 0", len(got))
	}
}

func TestHandleUsesProducersOwnFOV(t *testing.T) {
	ir := config.DriverCamera{Name: "ir", Gimbal: true, HFov: 30, VFov: 20}
	c, store := collector(rgb, ir)
	_ = store.SetSettings(Settings{RadiusM: 1, ExpiryS: 60, MinSightings: 1})
	c.Handle(result(t, "alpha.rgb", 1, 0.9, 0.5))
	c.Handle(result(t, "alpha.ir", 1, 0.9, 0.5))
	got := store.Snapshot(base)
	if len(got) != 2 {
		t.Fatalf("got %d pins, want 2 (different FOVs place the box apart)", len(got))
	}
}

func TestHandleNonDetectionIgnored(t *testing.T) {
	c, store := collector(rgb)
	_ = store.SetSettings(Settings{RadiusM: 5, ExpiryS: 60, MinSightings: 1})
	r := result(t, "alpha.rgb", 1, 0.5, 0.5)
	r.Type = "ClassificationResult"
	c.Handle(r)
	if got := store.Snapshot(base); len(got) != 0 {
		t.Fatalf("got %d pins from a classification, want 0", len(got))
	}
}

func str(s string) *string { return &s }

func TestRunSubscribesLocalDetectionEngines(t *testing.T) {
	var mu sync.Mutex
	catalog := flows.Catalog{Daemons: []flows.Daemon{
		{IP: "10.0.0.1", Local: true, Engines: []flows.Engine{
			{Name: "10.0.0.1.det", Result: "steeleagle_protocol/v1/messages/result/DetectionResult"},
			{Name: "10.0.0.1.det_ev", Events: []flows.Event{{Label: "result", Type: str("steeleagle_protocol/v1/messages/result/DetectionResult")}}},
			{Name: "10.0.0.1.cls", Result: "steeleagle_protocol/v1/messages/result/ClassificationResult"},
		}},
		{IP: "10.0.0.2", Engines: []flows.Engine{
			{Name: "10.0.0.2.det", Result: "steeleagle_protocol/v1/messages/result/DetectionResult"},
		}},
	}}
	src := func(context.Context) ([]fleet.Vehicle, flows.Catalog, error) {
		mu.Lock()
		defer mu.Unlock()
		return nil, catalog, nil
	}
	subs := map[string]context.Context{}
	subscribe := func(ctx context.Context, name string, _ func(feed.Result)) {
		mu.Lock()
		subs[name] = ctx
		mu.Unlock()
		<-ctx.Done()
	}
	c := NewCollector(NewStore(), NewPoseHistory(), zerolog.Nop())
	c.refresh = 10 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go c.Run(ctx, src, subscribe)

	waitFor(t, func() bool { mu.Lock(); defer mu.Unlock(); return len(subs) == 2 })
	mu.Lock()
	if subs["10.0.0.1.det"] == nil || subs["10.0.0.1.det_ev"] == nil {
		t.Fatalf("subscribed %v, want the two local detection engines", subs)
	}
	catalog.Daemons[0].Engines = catalog.Daemons[0].Engines[1:]
	detCtx := subs["10.0.0.1.det"]
	mu.Unlock()
	waitFor(t, func() bool { return detCtx.Err() != nil })
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatal("timed out")
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func TestHandleSkipsEmptyBoxes(t *testing.T) {
	c, store := collector(rgb)
	_ = store.SetSettings(Settings{RadiusM: 5, ExpiryS: 60, MinSightings: 1})
	for i, det := range []*resultpb.Detection{
		resultpb.Detection_builder{ClassName: "nobox"}.Build(),
		resultpb.Detection_builder{ClassName: "flat", Bbox: resultpb.BoundingBox_builder{XMin: 0.5, YMin: 0.5, XMax: 0.5, YMax: 0.6}.Build()}.Build(),
	} {
		b, err := protojson.Marshal(resultpb.DetectionResult_builder{Detections: []*resultpb.Detection{det}}.Build())
		if err != nil {
			t.Fatal(err)
		}
		r := result(t, "alpha.rgb", uint64(i+1), 0.5, 0.5)
		r.JSON = b
		c.Handle(r)
	}
	if got := store.Snapshot(base); len(got) != 0 {
		t.Fatalf("got %+v, want no pins from boxless detections", got)
	}
}
