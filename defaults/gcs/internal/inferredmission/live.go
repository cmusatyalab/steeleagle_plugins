package inferredmission

import (
	"bytes"
	"context"
	"errors"
	"io"
	"math"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/feed"
)

const (
	maxLiveBytes  = 16 << 20
	maxLiveFrames = 4096
)

type EncodedFrame struct {
	Producer, Vehicle      string
	Data                   []byte
	CapturedAt, ReceivedAt time.Time
	ID                     uint64
	Session, Seq           uint64
	Keyframe               bool
	Motion                 MotionEvidence
}

type liveInput struct {
	vehicle  string
	frames   chan EncodedFrame
	stop     chan struct{}
	stopOnce sync.Once
	dropped  atomic.Uint64
	evidence evidenceCache
}

func newLiveInput(vehicle string) *liveInput {
	return &liveInput{vehicle: vehicle, frames: make(chan EncodedFrame, 16), stop: make(chan struct{})}
}

func (in *liveInput) stopCapture() { in.stopOnce.Do(func() { close(in.stop) }) }

// Ingest enqueues an immutable frame from the GCS feed without blocking the
// WebRTC publisher. Only producers with active observations allocate work.
func (s *Service) Ingest(frame EncodedFrame) {
	if !s.mu.TryRLock() {
		return
	}
	in := s.live[frame.Producer]
	s.mu.RUnlock()
	if in == nil || in.vehicle != frame.Vehicle || len(frame.Data) == 0 || len(frame.Data) > maxLiveBytes/4 {
		return
	}
	select {
	case <-in.stop:
	case in.frames <- frame:
	default:
		in.dropped.Add(1)
	}
}

type livePacket struct {
	data      []byte
	at        time.Time
	capture   *float64
	id        uint64
	identity  frameIdentity
	motion    *MotionEvidence
	telemetry *nearbyMotion
	key       bool
}

type liveSection struct {
	row     section
	packets []livePacket
	indices []int
	origin  time.Time
}

func (s *Service) runLive(r *run, meta session) (string, string) {
	ctx, cancel := context.WithCancel(s.ctx)
	defer cancel()
	s.mu.RLock()
	source := s.liveDetector
	s.mu.RUnlock()
	if source.engine != nil && source.subscribe != nil {
		done := make(chan struct{})
		go func() {
			defer close(done)
			s.watchLiveDetection(ctx, r, meta, source)
		}()
		defer func() {
			cancel()
			<-done
		}()
	}
	jobs := make(chan liveSection, 2)
	collected := make(chan error, 1)
	go func() {
		defer close(jobs)
		collected <- collectLive(ctx, r, meta, jobs)
	}()
	for job := range jobs {
		if ctx.Err() != nil {
			break
		}
		job.row.Evidence = r.live.evidence.collect(ctx, meta.Producer, job)
		load := func() ([]sample, error) { return decodeLive(ctx, r, meta, job) }
		if err := s.process(r, meta, job.row, load); err != nil {
			cancel()
			<-collected
			s.log.Warn().Err(err).Msg("could not save live analysis")
			return "error", "Could not save the live section."
		}
	}
	if err := <-collected; err != nil {
		s.log.Warn().Err(err).Msg("could not archive live observation")
		return "error", "Could not archive the live observation."
	}
	if s.ctx.Err() != nil {
		return "stopped", "GCS stopped during observation."
	}
	return "stopped", ""
}

func (s *Service) watchLiveDetection(ctx context.Context, r *run, meta session, source liveDetectionSource) {
	lookup, cancel := context.WithTimeout(ctx, 5*time.Second)
	var engine string
	var err error
	if meta.Settings.Detector != "" {
		// A configured detector, on the GCS's daemon unless qualified
		engine = source.local(meta.Settings.Detector)
	} else {
		engine, err = source.engine(lookup, meta.Vehicle, meta.Producer)
	}
	cancel()
	if err != nil {
		s.log.Debug().Err(err).Str("vehicle", meta.Vehicle).Msg("live inference detector lookup failed")
		return
	}
	if engine == "" || ctx.Err() != nil {
		return
	}
	r.live.evidence.mu.Lock()
	r.live.evidence.engine = engine
	r.live.evidence.mu.Unlock()
	source.subscribe(ctx, engine, func(result feed.Result) {
		if result.Producer == meta.Producer && result.Vehicle == meta.Vehicle && result.Session != 0 {
			r.live.evidence.addResult(result)
		}
	})
}

func collectLive(ctx context.Context, r *run, meta session, jobs chan<- liveSection) error {
	in := r.live
	cfg := meta.Settings
	duration, interval := timeout(cfg.SectionSeconds), timeout(1/cfg.FrameFPS)
	sliding := cfg.SectionMode == sectionSliding
	stride := duration
	if sliding {
		stride = time.Second
	}
	var anchor, last, picked, lastPublishedSample time.Time
	lastSlot := -1
	var packets []livePacket
	var indices []int
	var telemetry map[string]any
	var partial bool
	var bytesUsed, index int
	var dropped uint64
	var lastID uint64
	var haveID, needKeyframe bool

	offset := func() float64 {
		if meta.Flight != nil {
			return math.Max(0, seconds(anchor)-*meta.Flight)
		}
		return 0
	}

	windowStart := func(index int) time.Duration {
		return time.Duration(index-1) * stride
	}

	publish := func(final bool) error {
		if index == 0 {
			return nil
		}
		start := windowStart(index).Seconds() + offset()
		end := start + cfg.SectionSeconds
		origin := anchor.Add(windowStart(index))
		if sliding && !lastPublishedSample.IsZero() && (len(indices) == 0 || !picked.After(lastPublishedSample)) {
			if final {
				return nil // Overlap alone does not make another observation.
			}
			return r.setSection(section{sectionSummary: sectionSummary{Index: index, Start: start, End: end,
				Status: "gap", Error: "No new sampled frames were received in this interval."}, InputQuality: "partial"})
		}
		quality := "complete"
		if partial || final || len(indices) < int(math.Ceil(cfg.SectionSeconds*cfg.FrameFPS/2)) {
			quality = "partial"
		}
		if final {
			end = math.Min(end, start+last.Sub(origin).Seconds()+0.001)
		}
		row := section{sectionSummary: sectionSummary{Index: index, Start: start, End: end, Status: "queued"},
			InputQuality: quality, Telemetry: telemetry}
		if len(indices) == 0 {
			row.Status, row.Error = "gap", "No decodable frames were available in this section."
			return r.setSection(row)
		}
		if len(jobs) == cap(jobs) {
			row.Status, row.Error = "gap", "Analysis fell behind the live stream. This section was skipped."
			return r.setSection(row)
		}
		if err := r.setSection(row); err != nil {
			return err
		}
		lastPublishedSample = picked
		// The next window may reuse the same packets. Give the serial
		// decoder its own slice headers and slots, sharing immutable bytes.
		jobs <- liveSection{row: row, packets: slices.Clone(packets), indices: slices.Clone(indices), origin: origin}
		return nil
	}

	advance := func(next int) {
		index = next
		origin := anchor.Add(windowStart(index))
		keep := 0
		for keep < len(indices) && packets[indices[keep]].at.Before(origin) {
			keep++
		}
		indices = indices[keep:]
		if len(indices) == 0 {
			picked, telemetry = time.Time{}, nil
		}
		// Keep the keyframe needed to decode the first retained sample.
		// If there are no retained samples, keep only the trailing GOP.
		first := len(packets) - 1
		if len(indices) > 0 {
			first = indices[0]
		}
		key := first
		for key >= 0 && !packets[key].key {
			key--
		}
		if key < 0 {
			packets, bytesUsed = nil, 0
		} else {
			for i := range key {
				bytesUsed -= len(packets[i].data)
			}
			clear(packets[:key])
			packets = packets[key:]
			for i := range indices {
				indices[i] -= key
			}
		}
		partial = false
	}

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-in.stop:
			return publish(true)
		default:
		}
		select {
		case <-ctx.Done():
			return nil
		case <-in.stop:
			return publish(true)
		case frame := <-in.frames:
			at := frame.CapturedAt
			var capture *float64
			if at.IsZero() {
				at = frame.ReceivedAt
			} else {
				stamp := seconds(at)
				capture = &stamp
			}
			if at.IsZero() || frame.ReceivedAt.Sub(at) > 30*time.Second ||
				at.Sub(frame.ReceivedAt) > 3*time.Second || (!last.IsZero() && !at.After(last)) {
				continue
			}
			if anchor.IsZero() {
				anchor = at
			}
			elapsed := at.Sub(anchor)
			next := 1
			if elapsed >= duration {
				next = int((elapsed-duration)/stride) + 2
			}
			if next > index {
				if err := publish(false); err != nil {
					return err
				}
				if index != 0 && next > index+1 {
					row := section{sectionSummary: sectionSummary{Index: index + 1, Status: "gap",
						Start: windowStart(index+1).Seconds() + offset(), End: (windowStart(next-1) + duration).Seconds() + offset(),
						Error: "No fresh frames were received in this interval."}, InputQuality: "partial"}
					if err := r.setSection(row); err != nil {
						return err
					}
				}
				advance(next)
				if frame.Keyframe && len(indices) == 0 {
					packets, bytesUsed = nil, 0
				}
			}
			if current := in.dropped.Load(); current != dropped || (haveID && frame.ID != lastID+1) {
				partial, dropped, needKeyframe = true, current, true
				if len(indices) == 0 {
					packets, bytesUsed = nil, 0
				}
			}
			last, lastID, haveID = at, frame.ID, true
			if needKeyframe {
				if !frame.Keyframe {
					continue
				}
				needKeyframe = false
			}
			if (len(packets) == 0 && !frame.Keyframe) || bytesUsed+len(frame.Data) > maxLiveBytes || len(packets) == maxLiveFrames {
				partial = true
				last = at
				continue
			}
			packets = append(packets, livePacket{data: frame.Data, at: at, capture: capture,
				id: frame.ID, identity: frameIdentity{frame.Session, frame.Seq}, key: frame.Keyframe})
			bytesUsed += len(frame.Data)
			slot := int(math.Floor(elapsed.Seconds()*cfg.FrameFPS + 1e-6))
			selected := picked.IsZero() || at.Sub(picked) >= interval-time.Microsecond
			if sliding {
				selected = slot > lastSlot
			}
			if selected {
				picked = at
				lastSlot = slot
				indices = append(indices, len(packets)-1)
				packet := &packets[len(packets)-1]
				if frame.Motion != (MotionEvidence{}) {
					motion := frame.Motion
					packet.motion = &motion
				}
				reading := in.evidence.nearby(at)
				packet.telemetry = reading
				if frame.Motion.RelativeAltitude != nil {
					telemetry = map[string]any{"relative_altitude_m": *frame.Motion.RelativeAltitude, "source": "imaging_frame"}
				} else if reading != nil && reading.RelativeAltitude != nil {
					telemetry = map[string]any{"relative_altitude_m": *reading.RelativeAltitude, "source": "telemetry_feed"}
				} else {
					telemetry = nil
				}
				if sliding && float64(slot+1)/cfg.FrameFPS >= (windowStart(index)+duration).Seconds() {
					if err := publish(false); err != nil {
						return err
					}
					advance(index + 1)
				}
			}
			if !selected {
				continue
			}
			r.mu.Lock()
			r.lastFrame = seconds(frame.ReceivedAt)
			r.collecting, r.frameCount = index, len(indices)
			r.notice = "Receiving timestamped imagery."
			if capture == nil {
				r.notice = "Receiving imagery using GCS receipt time."
			}
			r.update()
			r.mu.Unlock()
		}
	}
}

func decodeLive(ctx context.Context, r *run, meta session, job liveSection) ([]sample, error) {
	selector := make([]string, len(job.indices))
	for i, idx := range job.indices {
		selector[i] = "eq(n\\," + strconv.Itoa(idx) + ")"
	}
	filter := "select=" + strings.Join(selector, "+") + "," + scaleFilter(meta.Settings.MaxFrameSide) + ",showinfo"
	input := make([]io.Reader, 0, len(job.packets))
	for _, packet := range job.packets {
		input = append(input, bytes.NewReader(packet.data))
	}
	frames, err := decode(ctx, framesDir(r, job.row.Index), io.MultiReader(input...), []string{"-xerror", "-f", "h264", "-i", "pipe:0"},
		filter, len(job.indices), func(i int, _ []byte) (sample, error) {
			packet := job.packets[job.indices[i]]
			var frame sample
			frame.Time = packet.at.Sub(job.origin).Seconds()
			frame.Capture, frame.ID = packet.capture, &packet.id
			return frame, nil
		})
	if err != nil {
		return nil, err
	}
	if len(frames) != len(job.indices) {
		return nil, errors.New("Live frames could not be matched to their capture timestamps.")
	}
	return frames, nil
}
