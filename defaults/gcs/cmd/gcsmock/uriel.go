package main

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	framepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/frame"
	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/feed"
	"github.com/cmusatyalab/uriel/go"
	"github.com/rs/zerolog"
	"google.golang.org/protobuf/proto"
)

const (
	// RawFrameType is the payload type of the mock cameras' frames.
	RawFrameType = "steeleagle_protocol/v1/messages/frame/RawFrame"
	// flowName is the name of each vehicle's flow on its client.
	flowName = "flow"
	// encoderMaxOutput bounds an encoded frame: well above a keyframe of the
	// clips.
	encoderMaxOutput = 1 << 20
	// sinkMaxOutput is the output size of an engine that drops everything.
	sinkMaxOutput = 4096
)

// Uriel hosts the mock's Uriel server, its engines, and its vehicles' clients
// and producers. Engines and producers each run on their own goroutine, which
// they lock to its OS thread, as Uriel requires; the server and the clients
// may be used from any goroutine.
type Uriel struct {
	log    zerolog.Logger
	prefix string
	server *uriel.Server

	// clients are the vehicles' clients, by vehicle; fixed once the vehicles
	// are added.
	clients map[string]*vehicleClient

	producerCtx   context.Context
	stopProducers context.CancelFunc
	producers     sync.WaitGroup
	engineCtx     context.Context
	stopEngines   context.CancelFunc
	engines       sync.WaitGroup
	closeOnce     sync.Once
}

// vehicleClient is a vehicle's client and whether it has a flow.
type vehicleClient struct {
	mu      sync.Mutex
	client  *uriel.Client
	hasFlow bool
}

// NewUriel creates the server. prefix makes the names the GCS doesn't see
// unique to this process.
func NewUriel(prefix string, log zerolog.Logger) (*Uriel, error) {
	server, err := uriel.NewServer(serverName(prefix))
	if err != nil {
		return nil, fmt.Errorf("could not create the Uriel server: %w", err)
	}
	u := &Uriel{log: log, prefix: prefix, server: server, clients: map[string]*vehicleClient{}}
	u.producerCtx, u.stopProducers = context.WithCancel(context.Background())
	u.engineCtx, u.stopEngines = context.WithCancel(context.Background())
	return u, nil
}

func serverName(prefix string) string { return prefix + "-lcs" }

// EngineFunc answers one input of an engine with exactly one of Drop,
// Forward or Replace. It runs on the engine's goroutine.
type EngineFunc func(eng *uriel.Engine, in uriel.Input) error

// StartEngine creates the engine name, whose results are of resultType (empty
// for none), on its own goroutine, serves it, and hands it every input until
// Close. It returns once the server serves it.
func (u *Uriel) StartEngine(name, resultType string, maxOutput int, handle EngineFunc) error {
	ready := make(chan error, 1)
	u.engines.Add(1)
	go func() {
		defer u.engines.Done()
		eng, err := uriel.NewEngine(name, resultType, maxOutput)
		if err != nil {
			ready <- fmt.Errorf("could not create engine %q: %w", name, err)
			return
		}
		defer eng.Close()
		if err := u.server.AddEngine(name); err != nil {
			ready <- fmt.Errorf("could not serve engine %q: %w", name, err)
			return
		}
		ready <- nil
		for {
			in, err := eng.Next(u.engineCtx)
			if err != nil {
				if u.engineCtx.Err() == nil {
					u.log.Error().Err(err).Str("engine", name).Msg("engine stopped")
				}
				return
			}
			if err := handle(eng, in); err != nil {
				u.log.Warn().Err(err).Str("engine", name).Str("producer", in.ID.Producer).Msg("could not answer input")
			}
		}
	}()
	return <-ready
}

// ServeEngine has the server serve the engine name, which another process
// creates. Uriel accepts it before the engine exists, and delivers to it once
// it does.
func (u *Uriel) ServeEngine(name string) error {
	if err := u.server.AddEngine(name); err != nil {
		return fmt.Errorf("could not serve engine %q: %w", name, err)
	}
	return nil
}

// AddVehicle creates the vehicle's client, which takes the inputs of its
// producers, named by their short names.
func (u *Uriel) AddVehicle(vehicle string, producers ...string) error {
	client, err := uriel.NewClient(u.prefix+"-"+vehicle, serverName(u.prefix))
	if err != nil {
		return fmt.Errorf("could not create the client of %s: %w", vehicle, err)
	}
	for _, p := range producers {
		if err := client.AddProducer(vehicle + "." + p); err != nil {
			client.Close()
			return fmt.Errorf("could not add producer %s.%s: %w", vehicle, p, err)
		}
	}
	u.clients[vehicle] = &vehicleClient{client: client}
	return nil
}

// Apply replaces the vehicle's flow: it removes the one it has, if any, and
// adds flow. It implements FlowApplier.
func (u *Uriel) Apply(vehicle string, flow *config.FlowConfig) error {
	vc, ok := u.clients[vehicle]
	if !ok {
		return fmt.Errorf("no client for vehicle %q", vehicle)
	}
	spec, err := flow.ToBytes()
	if err != nil {
		return fmt.Errorf("could not encode the flow: %w", err)
	}
	vc.mu.Lock()
	defer vc.mu.Unlock()
	if vc.client == nil {
		return errors.New("the mock is shutting down")
	}
	if vc.hasFlow {
		if err := vc.client.RemoveFlow(flowName); err != nil {
			return fmt.Errorf("could not remove the flow of %s: %w", vehicle, err)
		}
		vc.hasFlow = false
	}
	if err := vc.client.AddFlow(flowName, string(spec)); err != nil {
		return fmt.Errorf("could not add the flow of %s: %w", vehicle, err)
	}
	vc.hasFlow = true
	u.log.Info().Str("vehicle", vehicle).Msg("applied flow")
	return nil
}

// StartProducer creates the producer name on its own goroutine, and every
// period publishes what next returns, until Close. It returns once the
// producer exists.
func (u *Uriel) StartProducer(
	name, payloadType string, maxSize int, period time.Duration, next func(now time.Time) ([]byte, error),
) error {
	ready := make(chan error, 1)
	u.producers.Add(1)
	go func() {
		defer u.producers.Done()
		p, err := uriel.NewProducer(name, payloadType, maxSize)
		if err != nil {
			ready <- fmt.Errorf("could not create producer %q: %w", name, err)
			return
		}
		defer p.Close()
		ready <- nil
		ticker := time.NewTicker(period)
		defer ticker.Stop()
		failing := false
		for {
			select {
			case <-u.producerCtx.Done():
				return
			case now := <-ticker.C:
				data, err := next(now)
				if err == nil {
					_, err = p.Send(data)
				}
				// Log a failure once, not once per tick
				if err != nil && !failing {
					u.log.Warn().Err(err).Str("producer", name).Msg("could not publish")
				}
				failing = err != nil
			}
		}
	}()
	return <-ready
}

// Close stops the producers, removes the flows with the clients, stops the
// engines and then the server.
func (u *Uriel) Close() {
	u.closeOnce.Do(func() {
		u.stopProducers()
		u.producers.Wait()
		for name, vc := range u.clients {
			vc.mu.Lock()
			if err := vc.client.Close(); err != nil {
				u.log.Warn().Err(err).Str("vehicle", name).Msg("could not close client")
			}
			vc.client = nil
			vc.mu.Unlock()
		}
		u.stopEngines()
		u.engines.Wait()
		if err := u.server.Close(); err != nil {
			u.log.Warn().Err(err).Msg("could not close the Uriel server")
		}
	})
}

// Encoder returns the handler of a mock h264_encoder: it replaces each raw
// frame with the next access unit of its vehicle's clip, looping the clip,
// and resuming at the next keyframe after a gap in the frames (see
// clipCursor). The vehicle is the one the input's producer belongs to. The
// handler keeps its place in each clip, so it must only be used by one
// engine.
func Encoder(clips map[string][]AccessUnit) EngineFunc {
	cursor := newClipCursor(clips)
	return func(eng *uriel.Engine, in uriel.Input) error {
		vehicle := feed.VehicleOf(in.ID.Producer)
		raw := &framepb.RawFrame{}
		if len(clips[vehicle]) == 0 {
			return errors.Join(fmt.Errorf("no clip for vehicle %q", vehicle), eng.Drop(nil))
		}
		if err := proto.Unmarshal(in.Data, raw); err != nil {
			return errors.Join(fmt.Errorf("could not decode raw frame: %w", err), eng.Drop(nil))
		}
		unit, _ := cursor.Next(vehicle, time.Now())
		out, err := proto.Marshal(framepb.EncodedFrame_builder{
			Timestamp:   raw.GetTimestamp(),
			Id:          raw.GetId(),
			CameraId:    raw.GetCameraId(),
			EncodedData: unit.Data,
			Codec:       framepb.Codec_CODEC_H264,
			Keyframe:    unit.Key,
		}.Build())
		if err == nil && len(out) > encoderMaxOutput {
			err = fmt.Errorf("encoded frame of %d bytes is over %d", len(out), encoderMaxOutput)
		}
		if err != nil {
			return errors.Join(err, eng.Drop(nil))
		}
		buf, err := eng.Loan()
		if err != nil {
			return errors.Join(err, eng.Drop(nil))
		}
		return eng.Replace(feed.FrameType, copy(buf, out), nil)
	}
}

// Sink drops every input.
func Sink(eng *uriel.Engine, in uriel.Input) error {
	return eng.Drop(nil)
}
