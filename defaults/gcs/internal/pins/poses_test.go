package pins

import (
	"testing"
	"time"

	"github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/common"
	framepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/frame"
	telemetrypb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/telemetry"
	"google.golang.org/protobuf/proto"
)

func positionInfo(lat, lon float64, z float32) *telemetrypb.PositionInfo {
	return telemetrypb.PositionInfo_builder{
		GlobalPosition: common.GlobalPosition_builder{
			Latitude: proto.Float64(lat), Longitude: proto.Float64(lon), Heading: proto.Float32(0),
		}.Build(),
		RelativePosition: common.RelativePosition_builder{Z: proto.Float32(z)}.Build(),
	}.Build()
}

func gimbalInfo(yaw, pitch float32) *telemetrypb.GimbalInfo {
	return telemetrypb.GimbalInfo_builder{
		PoseNeu: common.Pose_builder{Yaw: proto.Float32(yaw), Pitch: proto.Float32(pitch)}.Build(),
	}.Build()
}

func telemetry(lat, lon float64, z, yaw, pitch float32) *telemetrypb.Telemetry {
	return telemetrypb.Telemetry_builder{PositionInfo: positionInfo(lat, lon, z), GimbalInfo: gimbalInfo(yaw, pitch)}.Build()
}

func frame(lat, lon float64, z, yaw, pitch float32) *framepb.EncodedFrame {
	return framepb.EncodedFrame_builder{PositionInfo: positionInfo(lat, lon, z), GimbalInfo: gimbalInfo(yaw, pitch)}.Build()
}

func TestPoseFromTelemetry(t *testing.T) {
	tel := telemetry(40.2, -79.8, 20, 90, -30)
	got, ok := PoseFrom(tel.GetPositionInfo(), tel.GetGimbalInfo())
	if !ok || got != (CameraPose{Lat: 40.2, Lon: -79.8, Alt: 20, Yaw: 90, Pitch: -30}) {
		t.Fatalf("got %+v, %v", got, ok)
	}
}

func TestPoseNoFix(t *testing.T) {
	for name, tel := range map[string]*telemetrypb.Telemetry{
		"0,0":       telemetry(0, 0, 20, 0, -30),
		"on ground": telemetry(40.2, -79.8, 0, 0, -30),
		"no global": telemetrypb.Telemetry_builder{
			PositionInfo: telemetrypb.PositionInfo_builder{RelativePosition: common.RelativePosition_builder{Z: proto.Float32(20)}.Build()}.Build(),
			GimbalInfo:   gimbalInfo(0, -30),
		}.Build(),
		"no gimbal": telemetrypb.Telemetry_builder{PositionInfo: positionInfo(40.2, -79.8, 20)}.Build(),
	} {
		if _, ok := PoseFrom(tel.GetPositionInfo(), tel.GetGimbalInfo()); ok {
			t.Errorf("%s: ok, want no pose", name)
		}
	}
}

func TestLookupPrefersFrame(t *testing.T) {
	h := NewPoseHistory()
	now := base
	h.Telemetry("alpha", telemetry(40.2, -79.8, 20, 0, -30), now)
	h.Frame("alpha.rgb", 1, 7, frame(40.3, -79.7, 25, 45, -60))
	got, ok := h.Lookup("alpha.rgb", "alpha", 1, 7, now)
	if !ok || got.Lat != 40.3 || got.Pitch != -60 {
		t.Fatalf("got %+v, %v; want the frame's pose", got, ok)
	}
}

func TestLookupNearestTelemetry(t *testing.T) {
	h := NewPoseHistory()
	h.Telemetry("alpha", telemetry(40.2, -79.8, 20, 0, -30), base)
	h.Telemetry("alpha", telemetry(40.3, -79.8, 20, 0, -30), base.Add(400*time.Millisecond))
	got, ok := h.Lookup("alpha.rgb", "alpha", 1, 7, base.Add(300*time.Millisecond))
	if !ok || got.Lat != 40.3 {
		t.Fatalf("got %+v, %v; want the later telemetry", got, ok)
	}
}

func TestLookupNoTelemetryNearOffer(t *testing.T) {
	h := NewPoseHistory()
	h.Telemetry("alpha", telemetry(40.2, -79.8, 20, 0, -30), base)
	if got, ok := h.Lookup("alpha.rgb", "alpha", 1, 7, base.Add(1500*time.Millisecond)); ok {
		t.Fatalf("got %+v; want no pose 1.5 s from any telemetry", got)
	}
	if _, ok := h.Lookup("bravo.rgb", "bravo", 1, 7, base); ok {
		t.Fatal("got a pose for a vehicle with no telemetry")
	}
}

func TestLookupFrameRingBound(t *testing.T) {
	h := NewPoseHistory()
	for seq := uint64(1); seq <= 300; seq++ {
		h.Frame("alpha.rgb", 1, seq, frame(40.2, -79.8, 20, 0, -30))
	}
	if _, ok := h.Lookup("alpha.rgb", "alpha", 1, 1, base); ok {
		t.Error("frame 1 still held after 300 frames")
	}
	if _, ok := h.Lookup("alpha.rgb", "alpha", 1, 300, base); !ok {
		t.Error("frame 300 not held")
	}
}

func TestTelemetryHistoryBound(t *testing.T) {
	h := NewPoseHistory()
	h.Telemetry("alpha", telemetry(40.2, -79.8, 20, 0, -30), base)
	h.Telemetry("alpha", telemetry(40.3, -79.8, 20, 0, -30), base.Add(11*time.Second))
	if n := len(h.telemetry["alpha"]); n != 1 {
		t.Errorf("%d telemetry entries held, want 1 (10 s)", n)
	}
}

func TestLookupIgnoresOtherSession(t *testing.T) {
	h := NewPoseHistory()
	for seq := uint64(1); seq <= 10; seq++ {
		h.Frame("alpha.rgb", 1, seq, frame(40.3, -79.7, 25, 45, -60))
	}
	if got, ok := h.Lookup("alpha.rgb", "alpha", 2, 5, base); ok {
		t.Fatalf("got %+v from a previous session's frame 5", got)
	}
	h.Frame("alpha.rgb", 2, 1, frame(40.4, -79.7, 25, 45, -60))
	if _, ok := h.Lookup("alpha.rgb", "alpha", 1, 5, base); ok {
		t.Fatal("old session's frames kept after the producer restarted")
	}
	if got, ok := h.Lookup("alpha.rgb", "alpha", 2, 1, base); !ok || got.Lat != 40.4 {
		t.Fatalf("got %+v, %v; want the new session's frame", got, ok)
	}
}
