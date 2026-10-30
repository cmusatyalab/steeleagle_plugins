package feed

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"testing"
	"time"

	resultpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/result"
	"github.com/cmusatyalab/uriel/go"
	"github.com/rs/zerolog"
	"google.golang.org/protobuf/proto"
)

const detectionType = "steeleagle_protocol/v1/messages/result/DetectionResult"

func TestResultType(t *testing.T) {
	for _, tt := range []struct {
		payloadType string
		name        string
		ok          bool
	}{
		{detectionType, "DetectionResult", true},
		{"steeleagle_protocol/v1/messages/result/ClassificationResult", "ClassificationResult", true},
		{"messages/result/GuidanceResult", "GuidanceResult", true},
		{"steeleagle_protocol.v1.messages.result.PositionResult", "PositionResult", true},
		{"messages/result/GenericResult", "GenericResult", false},
		{"h264/v1/DecodeResult", "DecodeResult", false},
		{"", "", false},
	} {
		name, ok := ResultType(tt.payloadType)
		if name != tt.name || ok != tt.ok {
			t.Errorf("ResultType(%q) = %q, %v, want %q, %v", tt.payloadType, name, ok, tt.name, tt.ok)
		}
	}
}

func TestDecodeResult(t *testing.T) {
	data, _ := proto.Marshal(resultpb.DetectionResult_builder{Detections: []*resultpb.Detection{
		resultpb.Detection_builder{ClassName: "car", Score: 0.9, Bbox: resultpb.BoundingBox_builder{
			XMin: 0.1, YMin: 0.2, XMax: 0.3, YMax: 0.4,
		}.Build()}.Build(),
	}}.Build())
	r, err := DecodeResult("10.0.0.1.yolo", "drone1.front", detectionType, data)
	if err != nil {
		t.Fatal(err)
	}
	if r.Engine != "10.0.0.1.yolo" || r.Producer != "drone1.front" || r.Vehicle != "drone1" || r.Type != "DetectionResult" {
		t.Errorf("got %+v", r)
	}
	var got struct {
		Detections []struct {
			ClassName string  `json:"className"`
			Score     float64 `json:"score"`
			Bbox      struct {
				XMin, YMin, XMax, YMax float64
			} `json:"bbox"`
		} `json:"detections"`
	}
	if err := json.Unmarshal(r.JSON, &got); err != nil {
		t.Fatal(err)
	}
	if len(got.Detections) != 1 || got.Detections[0].ClassName != "car" || got.Detections[0].Bbox.XMax != 0.3 {
		t.Errorf("got JSON %s", r.JSON)
	}

	// An empty result still lists its (no) detections
	r, err = DecodeResult("e", "drone1.front", detectionType, nil)
	if err != nil || string(r.JSON) != `{"detections":[]}` {
		t.Errorf("DecodeResult(empty) = %s, %v, want no detections", r.JSON, err)
	}
	if _, err := DecodeResult("e", "p", "messages/result/GenericResult", nil); err == nil {
		t.Error("DecodeResult(GenericResult) succeeded, want an error")
	}
	if _, err := DecodeResult("e", "p", detectionType, []byte{0xff}); err == nil {
		t.Error("DecodeResult(invalid) succeeded, want an error")
	}
}

// TestSubscribeResults ensures that the results an engine forwards inputs
// with reach the handler, and generic ones don't.
func TestSubscribeResults(t *testing.T) {
	pid := os.Getpid()
	vehicle := fmt.Sprintf("gcs-results-vehicle-%d", pid)
	engine := fmt.Sprintf("gcs-results-%d", pid)
	producer := vehicle + ".front"

	lcs := fmt.Sprintf("gcs-results-lcs-%d", pid)
	server, err := uriel.NewServer(lcs)
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()
	if err := server.AddEngine(engine); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	detections, _ := proto.Marshal(resultpb.DetectionResult_builder{Detections: []*resultpb.Detection{
		resultpb.Detection_builder{ClassName: "car"}.Build(),
	}}.Build())
	// The engine forwards every other input with a generic result
	engineErr := make(chan error, 1)
	go func() {
		e, err := uriel.NewEngine(engine, detectionType, 4096)
		if err != nil {
			engineErr <- err
			return
		}
		defer e.Close()
		for {
			in, err := e.Next(ctx)
			if err != nil {
				engineErr <- nil
				return
			}
			result := detections
			if in.ID.Seq%2 == 0 {
				result = []byte{0xff} // not a DetectionResult, so it is skipped
			}
			if err := e.Forward(result); err != nil {
				engineErr <- err
				return
			}
		}
	}()

	results := make(chan Result, 16)
	done := make(chan struct{})
	go func() {
		defer close(done)
		SubscribeResults(ctx, engine, zerolog.Nop(), func(r Result) { results <- r })
	}()

	client, err := uriel.NewClient(vehicle, lcs)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	if err := client.AddProducer(producer); err != nil {
		t.Fatal(err)
	}
	spec := fmt.Sprintf(`
[[producers]]
name = %q

[[engines]]
name = %q
location = "local"

[[edges]]
from = %q
to = %q
timeout = "1s"
`, producer, engine, producer, engine)
	if err := client.AddFlow(vehicle, spec); err != nil {
		t.Fatal(err)
	}
	p, err := uriel.NewProducer(producer, FrameType, 4096)
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close()

	deadline := time.After(10 * time.Second)
	var r Result
receive:
	for {
		if _, err := p.Send([]byte("frame")); err != nil {
			t.Fatal(err)
		}
		select {
		case r = <-results:
			break receive
		case err := <-engineErr:
			t.Fatalf("engine failed: %v", err)
		case <-time.After(200 * time.Millisecond):
		case <-deadline:
			t.Fatal("no result reached the handler")
		}
	}
	if r.Engine != engine || r.Producer != producer || r.Vehicle != vehicle || r.Type != "DetectionResult" || r.Seq%2 == 0 {
		t.Errorf("got %+v, want a DetectionResult of %s", r, producer)
	}

	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("SubscribeResults did not return once its context was done")
	}
}
