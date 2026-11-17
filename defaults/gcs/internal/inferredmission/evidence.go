package inferredmission

import (
	"context"
	"math"
	"slices"
	"strings"
	"sync"
	"time"
	"unicode"

	resultpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/result"
	telemetrypb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/telemetry"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/feed"
	"google.golang.org/protobuf/encoding/protojson"
)

const (
	maxEvidenceResults = 4096
	maxMotionSamples   = 64
	maxDetectionLabels = 4
	detectionWait      = 200 * time.Millisecond
	telemetryAge       = 2 * time.Second
)

type liveDetectionSource struct {
	engine    func(context.Context, string, string) (string, error)
	subscribe func(context.Context, string, func(feed.Result))
}

// SetLiveDetectionSource configures a detector lookup and subscription for new
// live runs. The source is independent of viewers on the Control page.
func (s *Service) SetLiveDetectionSource(engine func(context.Context, string, string) (string, error), subscribe func(context.Context, string, func(feed.Result))) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.liveDetector = liveDetectionSource{engine: engine, subscribe: subscribe}
}

// MotionEvidence contains only the relative motion useful for inference.
// Missing values stay absent, including zero altitude and zero velocity
// when the driver did not report them.
type MotionEvidence struct {
	RelativeAltitude *float64 `json:"relative_altitude_m,omitempty"`
	HorizontalSpeed  *float64 `json:"horizontal_speed_m_s,omitempty"`
	VerticalSpeed    *float64 `json:"vertical_speed_m_s,omitempty"`
	GimbalPitch      *float64 `json:"gimbal_pitch_deg,omitempty"`
	GimbalYaw        *float64 `json:"gimbal_yaw_deg,omitempty"`
	Motion           string   `json:"motion_status,omitempty"`
	Mode             string   `json:"mode,omitempty"`
	Battery          *uint32  `json:"battery_percent,omitempty"`
}

func finiteValue(n float64, reported bool) *float64 {
	if !reported || math.IsNaN(n) || math.IsInf(n, 0) {
		return nil
	}
	round := math.Round(n*1000) / 1000
	if math.IsInf(round, 0) {
		return nil
	}
	return &round
}

// CameraMotion extracts frame-aligned measurements without including location.
func CameraMotion(position *telemetrypb.PositionInfo, gimbal *telemetrypb.GimbalInfo) MotionEvidence {
	var motion MotionEvidence
	if relative := position.GetRelativePosition(); relative != nil {
		motion.RelativeAltitude = finiteValue(float64(relative.GetZ()), relative.HasZ())
	}
	velocity := position.GetVelocityNeu()
	if velocity == nil {
		velocity = position.GetVelocityBody()
	}
	if velocity != nil {
		speed := math.Hypot(float64(velocity.GetXVel()), float64(velocity.GetYVel()))
		motion.HorizontalSpeed = finiteValue(speed, velocity.HasXVel() && velocity.HasYVel())
		motion.VerticalSpeed = finiteValue(float64(velocity.GetZVel()), velocity.HasZVel())
	}
	if pose := gimbal.GetPoseBody(); pose != nil {
		motion.GimbalPitch = finiteValue(float64(pose.GetPitch()), pose.HasPitch())
		motion.GimbalYaw = finiteValue(float64(pose.GetYaw()), pose.HasYaw())
	}
	return motion
}

type timedMotion struct {
	at     time.Time
	source string
	motion MotionEvidence
}

type nearbyMotion struct {
	MotionEvidence
	OffsetSec float64 `json:"offset_s"`
	Source    string  `json:"time_source"`
}

// IngestTelemetry retains new telemetry only for cameras under live analysis.
func (s *Service) IngestTelemetry(vehicle string, telemetry *telemetrypb.Telemetry, receivedAt time.Time) {
	if !s.mu.TryRLock() {
		return
	}
	defer s.mu.RUnlock()
	var reading *timedMotion
	for _, live := range s.live {
		if live.vehicle != vehicle {
			continue
		}
		if reading == nil {
			motion := CameraMotion(telemetry.GetPositionInfo(), telemetry.GetGimbalInfo())
			if telemetry.HasMotionStatus() && telemetry.GetMotionStatus() != telemetrypb.MotionStatus_MOTION_STATUS_UNSPECIFIED {
				motion.Motion = strings.TrimPrefix(telemetry.GetMotionStatus().String(), "MOTION_STATUS_")
			}
			if telemetry.HasMode() && telemetry.GetMode() != telemetrypb.Mode_MODE_UNSPECIFIED {
				motion.Mode = strings.TrimPrefix(telemetry.GetMode().String(), "MODE_")
			}
			if battery := telemetry.GetBatteryInfo(); battery.HasPercentage() && battery.GetPercentage() <= 100 {
				value := battery.GetPercentage()
				motion.Battery = &value
			}
			if motion == (MotionEvidence{}) {
				return
			}
			reading = &timedMotion{at: receivedAt, source: "receipt", motion: motion}
			if ts := telemetry.GetTimestamp(); ts != nil && ts.IsValid() {
				reading.at, reading.source = ts.AsTime(), "telemetry"
			}
		}
		live.evidence.addMotion(*reading)
	}
}

type frameIdentity struct {
	// A live input is already scoped to one producer. Session and sequence
	// complete the transport identity within it.
	session uint64
	seq     uint64
}

type detectorLabel struct {
	Class string  `json:"class"`
	Score float64 `json:"score"`
}

type frameEvidence struct {
	Frame            int             `json:"frame"`
	Time             float64         `json:"time_s"`
	Detections       []detectorLabel `json:"detections,omitempty"`
	FrameMotion      *MotionEvidence `json:"frame_motion,omitempty"`
	VehicleTelemetry *nearbyMotion   `json:"vehicle_telemetry,omitempty"`
}

type liveEvidence struct {
	Producer        string          `json:"producer"`
	Detector        string          `json:"detector,omitempty"`
	SampledFrames   int             `json:"sampled_frames"`
	DetectionFrames int             `json:"frames_with_detector_labels"`
	MotionFrames    int             `json:"frames_with_motion"`
	Frames          []frameEvidence `json:"frames,omitempty"`
}

type evidenceCache struct {
	mu       sync.Mutex
	engine   string
	results  map[frameIdentity][]detectorLabel
	keys     []frameIdentity
	nextKey  int
	motions  []timedMotion
	nextPose int
}

func (e *evidenceCache) addMotion(reading timedMotion) {
	// The vehicle data path also feeds the operator UI. Drop an optional
	// sample if the inference cache is busy and keep that path nonblocking.
	if !e.mu.TryLock() {
		return
	}
	defer e.mu.Unlock()
	if len(e.motions) < maxMotionSamples {
		e.motions = append(e.motions, reading)
	} else {
		e.motions[e.nextPose] = reading
		e.nextPose = (e.nextPose + 1) % maxMotionSamples
	}
}

func (e *evidenceCache) nearby(at time.Time) *nearbyMotion {
	e.mu.Lock()
	defer e.mu.Unlock()
	var chosen *timedMotion
	distance := telemetryAge
	for i := range e.motions {
		reading := &e.motions[i]
		if delta := reading.at.Sub(at).Abs(); delta < distance ||
			(delta == distance && (chosen == nil || chosen.at.Before(reading.at))) {
			chosen, distance = reading, delta
		}
	}
	if chosen == nil {
		return nil
	}
	return &nearbyMotion{MotionEvidence: chosen.motion, OffsetSec: chosen.at.Sub(at).Seconds(), Source: chosen.source}
}

func (e *evidenceCache) addResult(result feed.Result) {
	// Empty results may represent errors as well as zero detections. Keep only
	// positive observations so absence never means a frame was empty.
	if result.Type != "DetectionResult" || len(result.JSON) == 0 || len(result.JSON) > 65536 {
		return
	}
	var message resultpb.DetectionResult
	if err := protojson.Unmarshal(result.JSON, &message); err != nil {
		return
	}
	// Several boxes of one class should not displace other classes from a sample.
	scores := make(map[string]float64)
	for _, item := range message.GetDetections() {
		label, confidence := item.GetClassName(), item.GetScore()
		if label == "" || len(label) > 96 || strings.IndexFunc(label, unicode.IsControl) >= 0 ||
			math.IsNaN(confidence) || confidence < 0 || confidence > 1 {
			continue
		}
		if best, ok := scores[label]; !ok || confidence > best {
			scores[label] = confidence
		}
	}
	if len(scores) == 0 {
		return
	}
	labels := make([]detectorLabel, 0, len(scores))
	for label, score := range scores {
		labels = append(labels, detectorLabel{Class: label, Score: math.Round(score*1000) / 1000})
	}
	slices.SortFunc(labels, func(a, b detectorLabel) int {
		switch {
		case a.Score > b.Score:
			return -1
		case a.Score < b.Score:
			return 1
		default:
			return strings.Compare(a.Class, b.Class)
		}
	})
	if len(labels) > maxDetectionLabels {
		labels = slices.Clone(labels[:maxDetectionLabels])
	}

	e.mu.Lock()
	defer e.mu.Unlock()
	if result.Engine != e.engine {
		return
	}
	key := frameIdentity{result.Session, result.Seq}
	if e.results == nil {
		e.results = make(map[frameIdentity][]detectorLabel)
	}
	if _, exists := e.results[key]; exists {
		return
	}
	if len(e.keys) < maxEvidenceResults {
		e.keys = append(e.keys, key)
	} else {
		delete(e.results, e.keys[e.nextKey])
		e.keys[e.nextKey] = key
		e.nextKey = (e.nextKey + 1) % maxEvidenceResults
	}
	e.results[key] = labels
}

func (e *evidenceCache) snapshot(producer string, job liveSection) *liveEvidence {
	e.mu.Lock()
	defer e.mu.Unlock()
	record := &liveEvidence{Producer: producer, Detector: e.engine, SampledFrames: len(job.indices)}
	for i, index := range job.indices {
		p := &job.packets[index]
		frame := frameEvidence{Frame: i + 1, Time: p.at.Sub(job.origin).Seconds(),
			Detections: e.results[p.identity], FrameMotion: p.motion, VehicleTelemetry: p.telemetry}
		if len(frame.Detections) > 0 {
			record.DetectionFrames++
		}
		if frame.FrameMotion != nil || frame.VehicleTelemetry != nil {
			record.MotionFrames++
		}
		if len(frame.Detections) > 0 || frame.FrameMotion != nil || frame.VehicleTelemetry != nil {
			record.Frames = append(record.Frames, frame)
		}
	}
	return record
}

func (e *evidenceCache) collect(ctx context.Context, producer string, job liveSection) *liveEvidence {
	record := e.snapshot(producer, job)
	if record.Detector == "" || record.DetectionFrames == record.SampledFrames {
		return record
	}
	// Wait at most once for a section. Collection of new frames continues on
	// its own goroutine, and a missing detector never blocks analysis.
	timer := time.NewTimer(detectionWait)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return record
	case <-timer.C:
		return e.snapshot(producer, job)
	}
}
