package pins

import (
	"sync"
	"time"

	framepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/frame"
	telemetrypb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/telemetry"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
)

const (
	// framesKept is how many recent frames' poses are kept per camera.
	framesKept = 256
	// telemetryKept is how long telemetry poses are kept per vehicle.
	telemetryKept = 10 * time.Second
	// telemetryMatch is how near in time telemetry must be to a frame's
	// offer to stand in for the frame's own pose.
	telemetryMatch = time.Second
)

// PoseFrom is where the camera on gimbal g was, by position pos. It fails
// without a position fix (no global position, or 0,0), on the ground (a
// relative altitude of 0 or less), or without a gimbal pose.
func PoseFrom(pos *telemetrypb.PositionInfo, g *telemetrypb.GimbalInfo) (CameraPose, bool) {
	gp := pos.GetGlobalPosition()
	if gp == nil || (gp.GetLatitude() == 0 && gp.GetLongitude() == 0) {
		return CameraPose{}, false
	}
	alt := float64(pos.GetRelativePosition().GetZ())
	if alt <= 0 {
		return CameraPose{}, false
	}
	view := fleet.CameraViewOf(g, float64(gp.GetHeading()))
	if view == nil {
		return CameraPose{}, false
	}
	return CameraPose{Lat: gp.GetLatitude(), Lon: gp.GetLongitude(), Alt: alt, Yaw: view.Yaw, Pitch: view.Pitch}, true
}

type framePose struct {
	seq  uint64
	pose CameraPose
}

type timedPose struct {
	at   time.Time
	pose CameraPose
}

// PoseHistory keeps recent camera poses, from the frames and telemetry the
// GCS receives, to place detections that arrive later. It is safe for
// concurrent use.
type PoseHistory struct {
	mu sync.Mutex
	// frames are the recent frames' poses by camera producer, a ring of
	// framesKept, next being where the next goes.
	frames map[string]*frameRing
	// telemetry are the recent telemetry poses by vehicle, oldest first.
	telemetry map[string][]timedPose
}

// frameRing holds the recent frames of one session of a producer: its seqs
// start again when it restarts, in a new session.
type frameRing struct {
	session uint64
	poses   []framePose
	next    int
}

// NewPoseHistory returns an empty history.
func NewPoseHistory() *PoseHistory {
	return &PoseHistory{frames: map[string]*frameRing{}, telemetry: map[string][]timedPose{}}
}

// Frame records the pose of frame seq of the camera producer's session, if it
// has one. A new session replaces the frames of the last.
func (h *PoseHistory) Frame(producer string, session, seq uint64, f *framepb.EncodedFrame) {
	pose, ok := PoseFrom(f.GetPositionInfo(), f.GetGimbalInfo())
	if !ok {
		return
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	r := h.frames[producer]
	if r == nil || r.session != session {
		r = &frameRing{session: session}
		h.frames[producer] = r
	}
	if len(r.poses) < framesKept {
		r.poses = append(r.poses, framePose{seq, pose})
		return
	}
	r.poses[r.next] = framePose{seq, pose}
	r.next = (r.next + 1) % framesKept
}

// Telemetry records the vehicle's pose in t, received at received, if it
// has one, and forgets those over telemetryKept older.
func (h *PoseHistory) Telemetry(vehicle string, t *telemetrypb.Telemetry, received time.Time) {
	pose, ok := PoseFrom(t.GetPositionInfo(), t.GetGimbalInfo())
	if !ok {
		return
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	kept := append(h.telemetry[vehicle], timedPose{received, pose})
	i := 0
	for i < len(kept) && received.Sub(kept[i].at) > telemetryKept {
		i++
	}
	h.telemetry[vehicle] = kept[i:]
}

// Lookup returns the pose of frame seq of the camera producer's session on
// vehicle:
// the frame's own, if the GCS received it, else the vehicle's telemetry
// received nearest offered, if within telemetryMatch.
func (h *PoseHistory) Lookup(producer, vehicle string, session, seq uint64, offered time.Time) (CameraPose, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if r := h.frames[producer]; r != nil && r.session == session {
		for _, f := range r.poses {
			if f.seq == seq {
				return f.pose, true
			}
		}
	}
	var best *timedPose
	bestGap := telemetryMatch
	for i, p := range h.telemetry[vehicle] {
		gap := p.at.Sub(offered)
		if gap < 0 {
			gap = -gap
		}
		if gap <= bestGap {
			best, bestGap = &h.telemetry[vehicle][i], gap
		}
	}
	if best == nil {
		return CameraPose{}, false
	}
	return best.pose, true
}
