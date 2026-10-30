// Package rtc streams vehicles' frames and telemetry to browsers over WebRTC.
//
// Each browser connects with one or more peer connections. A peer connection
// carries the H.264 video of the cameras the browser asked for, one track per
// camera, and a data channel the browser opens, on which the GCS sends every
// vehicle's telemetry and the metadata of every frame as JSON (see
// [DataMessage]).
//
// On its data channel a browser may also pick an engine whose results it
// wants (see [ControlMessage]): it is then sent that engine's results about
// the inputs of the cameras it receives. The hub subscribes to an engine's
// results only while a viewer wants them.
//
// Frames are sent as they were encoded, so the encoder decides what the
// browser can decode: H.264 without B-frames, in a profile the browser
// supports, with SPS and PPS before every IDR frame. A browser starts
// decoding a camera at a keyframe, so a new viewer is first sent the frames
// since the camera's last keyframe, and a viewer that falls behind skips
// ahead to the next keyframe.
package rtc

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"sync"
	"time"

	framepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/frame"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/feed"
	"github.com/pion/webrtc/v4"
	"github.com/pion/webrtc/v4/pkg/media"
	"github.com/rs/zerolog"
	"google.golang.org/protobuf/encoding/protojson"
)

const (
	// maxGopFrames and maxGopBytes bound the frames kept since a camera's
	// last keyframe; past them, new viewers wait for the next keyframe.
	maxGopFrames = 300
	maxGopBytes  = 16 << 20
	// sinkQueue is how many frames a viewer's track may fall behind before
	// it skips ahead to the next keyframe; it holds a full replay.
	sinkQueue = maxGopFrames + 64
	// maxDataBuffered is how many bytes a data channel may have unsent
	// before messages to it are dropped.
	maxDataBuffered = 1 << 20
	// defaultFrameDuration is how long a frame lasts when the time to the
	// next one isn't known, and minFrameDuration and maxFrameDuration bound
	// the time between frames.
	defaultFrameDuration = 33 * time.Millisecond
	minFrameDuration     = time.Millisecond
	maxFrameDuration     = time.Second
	// replayFrameDuration is how long each replayed frame lasts, so a new
	// viewer catches up to the latest frame at once.
	replayFrameDuration = time.Millisecond
)

// H264Codec is the codec of video tracks: constrained baseline, which every
// browser decodes.
var H264Codec = webrtc.RTPCodecCapability{
	MimeType:    webrtc.MimeTypeH264,
	ClockRate:   90000,
	SDPFmtpLine: "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
}

// DataMessage is a message on a viewer's data channel.
type DataMessage struct {
	// Type is "telemetry", "frame" or "result".
	Type string `json:"type"`
	// Producer is the producer of the frame or telemetry, e.g.
	// "drone1.front" or "drone1.telemetry"; for a result, the producer of
	// the input it is about.
	Producer string `json:"producer"`
	Vehicle  string `json:"vehicle"`
	Seq      uint64 `json:"seq"`
	// Telemetry is the telemetry, as protobuf JSON.
	Telemetry json.RawMessage `json:"telemetry,omitempty"`
	// Frame is the frame without its encoded data, as protobuf JSON.
	Frame json.RawMessage `json:"frame,omitempty"`
	// Engine is the engine that produced the result, ResultType its message
	// name (e.g. "DetectionResult"), and Result the result as protobuf JSON.
	Engine     string          `json:"engine,omitempty"`
	ResultType string          `json:"result_type,omitempty"`
	Result     json.RawMessage `json:"result,omitempty"`
}

// ControlMessage is a message a browser sends on its data channel.
type ControlMessage struct {
	// Type is "results": the browser wants the results of Engine, or none
	// if Engine is empty.
	Type   string `json:"type"`
	Engine string `json:"engine"`
}

// ResultsFunc reads the results of the engine name, handing each to handle,
// until ctx is done.
type ResultsFunc func(ctx context.Context, name string, handle func(feed.Result))

// StreamInfo describes a producer the GCS has received from.
type StreamInfo struct {
	Producer string `json:"producer"`
	Vehicle  string `json:"vehicle"`
	// Kind is "video" or "telemetry".
	Kind     string    `json:"kind"`
	LastSeen time.Time `json:"last_seen"`
	Viewers  int       `json:"viewers"`
}

// Hub fans the frames and telemetry it is published out to viewers.
type Hub struct {
	api        *webrtc.API
	iceServers []webrtc.ICEServer
	log        zerolog.Logger

	// subscribe reads an engine's results; nil if they can't be read
	subscribe ResultsFunc
	// ctx is done once the hub is closed
	ctx    context.Context
	cancel context.CancelFunc

	mu        sync.Mutex
	closed    bool
	videos    map[string]*video     // by producer
	telemetry map[string]*telemetry // by producer
	viewers   map[*viewer]struct{}
	results   map[string]*results // by engine
}

// results is a subscription to an engine's results, shared by the viewers
// that want them.
type results struct {
	cancel  context.CancelFunc
	viewers int
}

// video is a camera's H.264 stream.
type video struct {
	vehicle  string
	lastSeen time.Time
	// last is the capture time of the camera's last frame, if any
	last time.Time
	// gop is every frame since the last keyframe, or nil if they weren't all
	// kept
	gop      []media.Sample
	gopBytes int
	sinks    map[*sink]struct{}
	// warned is whether a frame of another codec was logged
	warned bool
}

// telemetry is the last telemetry of a vehicle, which new viewers are sent.
type telemetry struct {
	vehicle  string
	lastSeen time.Time
	message  []byte
}

// sink is a viewer's track of a video. Frames queue for its writer, which
// writes them to the track.
type sink struct {
	track *webrtc.TrackLocalStaticSample
	queue chan media.Sample
	// waitKey is whether the sink skips frames until the next keyframe,
	// because it lost one; guarded by the hub's mu
	waitKey bool
}

// Options configures a hub.
type Options struct {
	// ICEServers are STUN or TURN URLs, e.g. "stun:stun.l.google.com:19302".
	ICEServers []string
	// PortMin and PortMax bound the UDP ports WebRTC is sent from; zero for
	// any port.
	PortMin, PortMax uint16
	// Results reads an engine's results for the viewers that want them; nil
	// if engines' results can't be read.
	Results ResultsFunc
}

// NewHub returns a hub with options.
func NewHub(options Options, log zerolog.Logger) (*Hub, error) {
	var settings webrtc.SettingEngine
	if options.PortMin != 0 || options.PortMax != 0 {
		if err := settings.SetEphemeralUDPPortRange(options.PortMin, options.PortMax); err != nil {
			return nil, fmt.Errorf("invalid WebRTC port range: %w", err)
		}
	}
	var iceServers []webrtc.ICEServer
	if len(options.ICEServers) > 0 {
		iceServers = []webrtc.ICEServer{{URLs: options.ICEServers}}
	}
	ctx, cancel := context.WithCancel(context.Background())
	return &Hub{
		api:        webrtc.NewAPI(webrtc.WithSettingEngine(settings)),
		iceServers: iceServers,
		log:        log,
		subscribe:  options.Results,
		ctx:        ctx,
		cancel:     cancel,
		videos:     map[string]*video{},
		telemetry:  map[string]*telemetry{},
		viewers:    map[*viewer]struct{}{},
		results:    map[string]*results{},
	}, nil
}

// Publish sends msg to every viewer: telemetry and frame metadata on their
// data channels, and frames on the tracks of viewers of their camera. It
// never blocks on a viewer.
func (h *Hub) Publish(msg feed.Message) {
	switch {
	case msg.Telemetry != nil:
		h.publishTelemetry(msg)
	case msg.Frame != nil:
		h.publishFrame(msg)
	}
}

func (h *Hub) publishTelemetry(msg feed.Message) {
	data, err := protojson.Marshal(msg.Telemetry)
	if err != nil {
		h.log.Warn().Err(err).Str("producer", msg.Producer).Msg("could not encode telemetry")
		return
	}
	message, err := json.Marshal(DataMessage{
		Type: "telemetry", Producer: msg.Producer, Vehicle: msg.Vehicle, Seq: msg.Seq, Telemetry: data,
	})
	if err != nil {
		h.log.Warn().Err(err).Str("producer", msg.Producer).Msg("could not encode telemetry")
		return
	}
	h.mu.Lock()
	h.telemetry[msg.Producer] = &telemetry{vehicle: msg.Vehicle, lastSeen: time.Now(), message: message}
	channels := h.channels()
	h.mu.Unlock()
	send(channels, message)
}

func (h *Hub) publishFrame(msg feed.Message) {
	frame := msg.Frame
	// The frame's metadata, without its encoded data
	meta := framepb.EncodedFrame_builder{
		Timestamp:    frame.GetTimestamp(),
		Id:           frame.GetId(),
		GimbalInfo:   frame.GetGimbalInfo(),
		PositionInfo: frame.GetPositionInfo(),
		CameraId:     frame.GetCameraId(),
		Codec:        frame.GetCodec(),
		Keyframe:     frame.GetKeyframe(),
	}.Build()
	var message []byte
	data, err := protojson.Marshal(meta)
	if err == nil {
		message, err = json.Marshal(DataMessage{
			Type: "frame", Producer: msg.Producer, Vehicle: msg.Vehicle, Seq: msg.Seq, Frame: data,
		})
	}
	if err != nil {
		h.log.Warn().Err(err).Str("producer", msg.Producer).Msg("could not encode frame metadata")
	}

	h.mu.Lock()
	v := h.video(msg.Producer, msg.Vehicle)
	v.lastSeen = time.Now()
	if frame.GetCodec() == framepb.Codec_CODEC_H264 {
		v.write(frame)
	} else if !v.warned {
		v.warned = true
		h.log.Warn().Str("producer", msg.Producer).Stringer("codec", frame.GetCodec()).
			Msg("frames that aren't H.264 can't be streamed; only their metadata is sent")
	}
	channels := h.channels()
	h.mu.Unlock()
	if message != nil {
		send(channels, message)
	}
}

// PublishResult sends r to the viewers that want the results of its engine
// and receive the video of its producer. It never blocks on a viewer.
func (h *Hub) PublishResult(r feed.Result) {
	message, err := json.Marshal(DataMessage{
		Type: "result", Producer: r.Producer, Vehicle: r.Vehicle, Seq: r.Seq,
		Engine: r.Engine, ResultType: r.Type, Result: r.JSON,
	})
	if err != nil {
		h.log.Warn().Err(err).Str("engine", r.Engine).Msg("could not encode result")
		return
	}
	h.mu.Lock()
	var channels []*webrtc.DataChannel
	for v := range h.viewers {
		if v.data == nil || v.engine != r.Engine {
			continue
		}
		if _, ok := v.producers[r.Producer]; ok {
			channels = append(channels, v.data)
		}
	}
	h.mu.Unlock()
	send(channels, message)
}

// wantResults makes v want the results of engine, or none if engine is
// empty, subscribing to the engine's results if no other viewer wants them
// and unsubscribing from the ones v wanted before if none still does. The
// caller must hold mu.
func (h *Hub) wantResults(v *viewer, engine string) {
	if v.engine == engine {
		return
	}
	if old, ok := h.results[v.engine]; ok {
		old.viewers--
		if old.viewers == 0 {
			old.cancel()
			delete(h.results, v.engine)
		}
	}
	v.engine = engine
	if engine == "" || h.subscribe == nil {
		return
	}
	if r, ok := h.results[engine]; ok {
		r.viewers++
		return
	}
	ctx, cancel := context.WithCancel(h.ctx)
	h.results[engine] = &results{cancel: cancel, viewers: 1}
	go h.subscribe(ctx, engine, h.PublishResult)
}

// video returns the video of producer, adding it if it is new. The caller
// must hold mu.
func (h *Hub) video(producer, vehicle string) *video {
	v, ok := h.videos[producer]
	if !ok {
		v = &video{vehicle: vehicle, sinks: map[*sink]struct{}{}}
		h.videos[producer] = v
	}
	return v
}

// write sends an H.264 frame to the video's sinks, and keeps it for new
// viewers. The caller must hold the hub's mu.
func (v *video) write(frame *framepb.EncodedFrame) {
	duration := defaultFrameDuration
	at := frame.GetTimestamp().AsTime()
	if frame.HasTimestamp() && !v.last.IsZero() {
		duration = min(max(at.Sub(v.last), minFrameDuration), maxFrameDuration)
	}
	if frame.HasTimestamp() {
		v.last = at
	}
	// A sample's duration is the time until the next one, which isn't known
	// yet: the time since the last frame stands in for it
	sample := media.Sample{Data: frame.GetEncodedData(), Duration: duration}

	keyframe := frame.GetKeyframe()
	switch {
	case keyframe:
		v.gop, v.gopBytes = []media.Sample{sample}, len(sample.Data)
	case v.gop != nil && len(v.gop) < maxGopFrames && v.gopBytes+len(sample.Data) <= maxGopBytes:
		v.gop = append(v.gop, sample)
		v.gopBytes += len(sample.Data)
	default:
		v.gop, v.gopBytes = nil, 0
	}
	for s := range v.sinks {
		if keyframe {
			s.waitKey = false
		}
		if !s.waitKey {
			s.push(sample)
		}
	}
}

// push queues sample for the sink's writer, or makes the sink wait for the
// next keyframe if its queue is full. The caller must hold the hub's mu.
func (s *sink) push(sample media.Sample) {
	select {
	case s.queue <- sample:
	default:
		s.waitKey = true
	}
}

// attach starts sending the video of producer to s: the frames since its
// last keyframe first, if they were all kept, or else from its next
// keyframe.
func (h *Hub) attach(producer string, s *sink) {
	h.mu.Lock()
	defer h.mu.Unlock()
	v := h.video(producer, feed.VehicleOf(producer))
	v.sinks[s] = struct{}{}
	if v.gop == nil {
		s.waitKey = true
		return
	}
	for _, sample := range v.gop {
		sample.Duration = replayFrameDuration
		s.push(sample)
	}
}

// detach stops sending the video of producer to s.
func (h *Hub) detach(producer string, s *sink) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if v, ok := h.videos[producer]; ok {
		delete(v.sinks, s)
	}
}

// channels returns the open data channels of every viewer. The caller must
// hold mu.
func (h *Hub) channels() []*webrtc.DataChannel {
	var channels []*webrtc.DataChannel
	for v := range h.viewers {
		if v.data != nil {
			channels = append(channels, v.data)
		}
	}
	return channels
}

// send sends message on every channel that isn't too far behind.
func send(channels []*webrtc.DataChannel, message []byte) {
	for _, dc := range channels {
		if dc.BufferedAmount() > maxDataBuffered {
			continue
		}
		// A channel that just closed fails, which its viewer handles
		_ = dc.SendText(string(message))
	}
}

// Streams describes every producer the hub has received from, sorted by
// producer.
func (h *Hub) Streams() []StreamInfo {
	h.mu.Lock()
	defer h.mu.Unlock()
	var streams []StreamInfo
	for producer, v := range h.videos {
		if v.lastSeen.IsZero() {
			continue // only asked for by a viewer
		}
		streams = append(streams, StreamInfo{
			Producer: producer, Vehicle: v.vehicle, Kind: "video", LastSeen: v.lastSeen, Viewers: len(v.sinks),
		})
	}
	for producer, t := range h.telemetry {
		streams = append(streams, StreamInfo{
			Producer: producer, Vehicle: t.vehicle, Kind: "telemetry", LastSeen: t.lastSeen, Viewers: len(h.viewers),
		})
	}
	slices.SortFunc(streams, func(a, b StreamInfo) int { return strings.Compare(a.Producer, b.Producer) })
	return streams
}

// Close disconnects every viewer, and stops accepting new ones.
func (h *Hub) Close() error {
	h.cancel()
	h.mu.Lock()
	h.closed = true
	viewers := make([]*viewer, 0, len(h.viewers))
	for v := range h.viewers {
		viewers = append(viewers, v)
	}
	h.mu.Unlock()
	for _, v := range viewers {
		v.close()
	}
	return nil
}
