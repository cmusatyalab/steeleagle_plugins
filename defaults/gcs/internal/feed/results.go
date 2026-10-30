package feed

import (
	"context"
	"fmt"
	"strings"
	"time"

	resultpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/result"
	"github.com/cmusatyalab/uriel/go"
	"github.com/rs/zerolog"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// resubscribeDelay is how long Subscribe waits before subscribing to an engine
// again after its subscription fails.
const resubscribeDelay = 2 * time.Second

// Result is a result an engine produced for an input, decoded to protobuf
// JSON.
type Result struct {
	// Engine is the engine that produced the result.
	Engine string
	// Producer is the producer of the input the result is about, e.g.
	// "drone1.front", and Vehicle the vehicle it belongs to.
	Producer string
	Vehicle  string
	Seq      uint64
	// Type is the result's message type, e.g. "DetectionResult".
	Type string
	// JSON is the result as protobuf JSON.
	JSON []byte
}

// displayable are the result messages the GCS can show, by their message
// name; any other result, e.g. a GenericResult, isn't decoded.
var displayable = map[string]func() proto.Message{
	"DetectionResult":      func() proto.Message { return &resultpb.DetectionResult{} },
	"ClassificationResult": func() proto.Message { return &resultpb.ClassificationResult{} },
	"GuidanceResult":       func() proto.Message { return &resultpb.GuidanceResult{} },
	"PositionResult":       func() proto.Message { return &resultpb.PositionResult{} },
}

// ResultType returns the message name of payloadType, e.g. "DetectionResult"
// for "steeleagle_protocol/v1/messages/result/DetectionResult", and whether
// the GCS can show it.
func ResultType(payloadType string) (string, bool) {
	name := payloadType[strings.LastIndexAny(payloadType, "/.")+1:]
	_, ok := displayable[name]
	return name, ok
}

// DecodeResult reads an engine's result data of payloadType about an input of
// producer. It fails for a result the GCS can't show.
func DecodeResult(engine, producer, payloadType string, data []byte) (Result, error) {
	name, ok := ResultType(payloadType)
	if !ok {
		return Result{}, fmt.Errorf("unsupported result type %q", payloadType)
	}
	m := displayable[name]()
	if err := proto.Unmarshal(data, m); err != nil {
		return Result{}, fmt.Errorf("could not decode %s: %w", payloadType, err)
	}
	js, err := protojson.MarshalOptions{EmitDefaultValues: true}.Marshal(m)
	if err != nil {
		return Result{}, fmt.Errorf("could not encode %s: %w", payloadType, err)
	}
	return Result{Engine: engine, Producer: producer, Vehicle: VehicleOf(producer), Type: name, JSON: js}, nil
}

// SubscribeResults reads the results of the local Uriel engine name, and hands
// each one the GCS can show to handle, until ctx is done. A failed
// subscription, e.g. to an engine that isn't running yet, is retried. It keeps
// its goroutine on one OS thread, as Uriel requires.
func SubscribeResults(ctx context.Context, name string, log zerolog.Logger, handle func(Result)) {
	log = log.With().Str("engine", name).Logger()
	for ctx.Err() == nil {
		err := subscribeResults(ctx, name, log, handle)
		if err == nil || ctx.Err() != nil {
			return
		}
		log.Warn().Err(err).Msg("results subscription failed; retrying")
		select {
		case <-ctx.Done():
		case <-time.After(resubscribeDelay):
		}
	}
}

func subscribeResults(ctx context.Context, name string, log zerolog.Logger, handle func(Result)) error {
	sub, err := uriel.SubscribeEngine(name)
	if err != nil {
		return fmt.Errorf("could not subscribe to engine %q: %w", name, err)
	}
	defer sub.Close()
	log.Debug().Msg("reading results")
	warned := map[string]bool{}
	for {
		ev, err := sub.Next(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return fmt.Errorf("could not read event: %w", err)
		}
		if (ev.Kind != uriel.EventResult && ev.Kind != uriel.EventDerived) || len(ev.Result) == 0 {
			continue
		}
		if _, ok := ResultType(ev.PayloadType); !ok {
			continue
		}
		r, err := DecodeResult(name, ev.ID.Producer, ev.PayloadType, ev.Result)
		if err != nil {
			if !warned[ev.PayloadType] {
				warned[ev.PayloadType] = true
				log.Warn().Err(err).Str("producer", ev.ID.Producer).Msg("skipping results")
			}
			continue
		}
		r.Seq = ev.ID.Seq
		handle(r)
	}
}
