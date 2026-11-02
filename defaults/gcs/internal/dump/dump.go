// Package dump records the H.264 frames the GCS receives from Uriel, before
// they are sent to browsers, to tell what Uriel delivered apart from what
// WebRTC and the browser make of it.
//
// Each camera gets two files in the dump directory, named for its producer and
// the time recording started:
//
//   - <producer>-<time>.h264: every frame's encoded data, in the order it
//     arrived, as an Annex B stream that ffplay or VLC can play.
//   - <producer>-<time>.csv: one row per frame: when it arrived, when it was
//     captured, its Uriel sequence number, its frame id, whether it is a
//     keyframe, its size, and how many frames were missed before it.
//
// The H.264 encoder numbers the frames of a stream one after another in
// EncodedFrame.id, so a jump in the id means frames were lost on the way, for
// example skipped by Uriel for newer ones. Each jump is also logged.
package dump

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"

	framepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/frame"
	"github.com/rs/zerolog"
)

// header is the first row of every CSV file.
const header = "recv_unix_ns,capture_unix_ns,latency_ms,uriel_seq,frame_id,keyframe,bytes,missed_frames\n"

// Recorder writes the frames of each camera to files in a directory. Its
// methods may be called from any goroutine.
type Recorder struct {
	dir string
	log zerolog.Logger

	mu      sync.Mutex
	streams map[string]*stream // by producer
}

// stream is the files of one camera.
type stream struct {
	h264, csv *os.File
	// next is the frame id expected next; valid once started
	next    uint64
	started bool
	// failed is set when a write fails, after which the stream is no longer
	// recorded
	failed bool
}

// New returns a recorder that writes to dir, creating it if needed.
func New(dir string, log zerolog.Logger) (*Recorder, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("could not create dump directory: %w", err)
	}
	return &Recorder{dir: dir, log: log, streams: map[string]*stream{}}, nil
}

// Write records frame, which arrived from producer at time at with the Uriel
// sequence number seq. Frames that aren't H.264 are not recorded.
func (r *Recorder) Write(producer string, seq uint64, frame *framepb.EncodedFrame, at time.Time) {
	if frame.GetCodec() != framepb.Codec_CODEC_H264 {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()

	s, ok := r.streams[producer]
	if !ok {
		var err error
		s, err = r.open(producer, at)
		if err != nil {
			r.log.Error().Err(err).Str("producer", producer).Msg("could not start recording frames")
			s = &stream{failed: true}
		}
		r.streams[producer] = s
	}
	if s.failed {
		return
	}

	id := frame.GetId()
	var missed uint64
	switch {
	case !s.started:
	case id > s.next:
		missed = id - s.next
		r.log.Warn().Str("producer", producer).Uint64("expected", s.next).Uint64("got", id).
			Uint64("missed", missed).Uint64("seq", seq).Msg("frames missing before this one")
	case id < s.next:
		r.log.Info().Str("producer", producer).Uint64("expected", s.next).Uint64("got", id).
			Msg("encoder restarted its stream")
	}
	s.started, s.next = true, id+1

	var capture, latency string
	if frame.HasTimestamp() {
		c := frame.GetTimestamp().AsTime()
		capture = strconv.FormatInt(c.UnixNano(), 10)
		latency = strconv.FormatFloat(float64(at.Sub(c))/float64(time.Millisecond), 'f', 1, 64)
	}
	data := frame.GetEncodedData()
	row := fmt.Sprintf("%d,%s,%s,%d,%d,%t,%d,%d\n",
		at.UnixNano(), capture, latency, seq, id, frame.GetKeyframe(), len(data), missed)

	// One write per file per frame, unbuffered, so the files are complete up
	// to the last frame however the GCS stops
	if _, err := s.h264.Write(data); err != nil {
		r.fail(producer, s, err)
		return
	}
	if _, err := s.csv.WriteString(row); err != nil {
		r.fail(producer, s, err)
	}
}

// open creates the files of producer's stream, named for the time at.
func (r *Recorder) open(producer string, at time.Time) (*stream, error) {
	base := filepath.Join(r.dir, fileName(producer)+"-"+at.Format("20060102-150405"))
	h264, err := os.Create(base + ".h264")
	if err != nil {
		return nil, err
	}
	csv, err := os.Create(base + ".csv")
	if err != nil {
		h264.Close()
		return nil, err
	}
	if _, err := csv.WriteString(header); err != nil {
		h264.Close()
		csv.Close()
		return nil, err
	}
	r.log.Info().Str("producer", producer).Str("files", base+".{h264,csv}").Msg("recording frames")
	return &stream{h264: h264, csv: csv}, nil
}

// fail stops recording s after a write failed. The caller must hold mu.
func (r *Recorder) fail(producer string, s *stream, err error) {
	r.log.Error().Err(err).Str("producer", producer).Msg("could not record frame; no longer recording this camera")
	s.failed = true
	s.close()
}

func (s *stream) close() {
	if s.h264 != nil {
		s.h264.Close()
	}
	if s.csv != nil {
		s.csv.Close()
	}
}

// Close closes every file.
func (r *Recorder) Close() {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, s := range r.streams {
		if !s.failed {
			s.close()
		}
	}
}

// fileName returns producer with every character that can't safely be in a
// file name replaced with an underscore.
func fileName(producer string) string {
	b := []byte(producer)
	for i, c := range b {
		switch {
		case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9', c == '.', c == '-', c == '_':
		default:
			b[i] = '_'
		}
	}
	return string(b)
}
