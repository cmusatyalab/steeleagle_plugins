package inferredmission

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"image"
	"image/jpeg"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/feed"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/flows"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/nl2dsl"
	"github.com/rs/zerolog"
)

func TestV6DetectorEvidenceChangesCompiledMission(t *testing.T) {
	const (
		vehicle  = detectorTestIP + ".drone"
		producer = vehicle + ".rear"
		detector = detectorTestIP + ".rear_detector"
		target   = "Inspection Marker: Red"
		track    = `Track the "Inspection Marker: Red", then return home. If the battery drops to 30 percent, return home.`
		wait     = "Wait 30 seconds, then return home."
	)
	archivedPrompt, err := os.ReadFile("prompts/v6.md")
	if err != nil {
		t.Fatal(err)
	}
	var frame bytes.Buffer
	if err := jpeg.Encode(&frame, image.NewGray(image.Rect(0, 0, 8, 8)), nil); err != nil {
		t.Fatal(err)
	}
	frameURL := "data:image/jpeg;base64," + base64.StdEncoding.EncodeToString(frame.Bytes())
	at := time.Unix(100, 0)
	job := liveSection{origin: at, indices: []int{0}, packets: []livePacket{{
		at: at, identity: frameIdentity{2, 7}, motion: &MotionEvidence{RelativeAltitude: new(5.0)},
	}}}

	for _, tc := range []struct {
		name, resultProducer, label string
		streamSession               uint64
		wantLabel, wantTrack        bool
	}{
		{name: "matched label", resultProducer: producer, label: target, streamSession: 2, wantLabel: true, wantTrack: true},
		{name: "no detector result"},
		{name: "previous stream session", resultProducer: producer, label: target, streamSession: 1},
		{name: "other camera", resultProducer: vehicle + ".front", label: target, streamSession: 2},
		{name: "different class spelling", resultProducer: producer, label: "Inspection Marker: red", streamSession: 2, wantLabel: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			calls := 0
			// Hold the vision recommendation fixed so any change to the built
			// mission comes from the detector evidence and route selection.
			provider := detectorTestProvider(func(req *http.Request) (*http.Response, error) {
				calls++
				var body struct {
					Instructions string `json:"instructions"`
					Input        []struct {
						Content []struct {
							Text string `json:"text"`
							URL  string `json:"image_url"`
						} `json:"content"`
					} `json:"input"`
				}
				if err := json.NewDecoder(req.Body).Decode(&body); err != nil || len(body.Input) != 1 {
					t.Fatalf("expected one vision request, got %d input messages: %v", len(body.Input), err)
				}
				content := body.Input[0].Content
				if len(content) != 2 || content[0].Text != "Frame 1 at 0.000 seconds from section start" || content[1].URL != frameURL {
					t.Fatal("expected the same frame and timestamp in each vision request")
				}
				basePrompt, evidenceJSON, ok := strings.Cut(body.Instructions, "\n\n## Current evidence and runtime capabilities\n")
				if !ok || basePrompt != strings.TrimSpace(string(archivedPrompt)) {
					t.Fatal("missing the V6 instructions and structured evidence")
				}
				var prompt struct {
					Evidence *liveEvidence  `json:"live_evidence"`
					Caps     v6Capabilities `json:"runtime_capabilities"`
				}
				if err := json.Unmarshal([]byte(evidenceJSON), &prompt); err != nil {
					t.Fatal(err)
				}
				evidence := prompt.Evidence
				if evidence == nil || evidence.Producer != producer || evidence.Detector != detector ||
					evidence.SampledFrames != 1 || evidence.MotionFrames != 1 || len(evidence.Frames) != 1 || len(prompt.Caps.Track) != 2 {
					t.Fatalf("expected one sampled frame with motion and two camera routes: %+v, %+v", evidence, prompt.Caps.Track)
				}
				frame := evidence.Frames[0]
				if frame.Frame != 1 || frame.Time != 0 || frame.FrameMotion == nil ||
					frame.FrameMotion.RelativeAltitude == nil || *frame.FrameMotion.RelativeAltitude != 5 {
					t.Fatalf("expected identical frame timing and telemetry, got %+v", frame)
				}
				labels := frame.Detections
				if tc.wantLabel {
					if evidence.DetectionFrames != 1 || len(labels) != 1 || labels[0] != (detectorLabel{ID: 1, Class: tc.label, Score: 0.89}) {
						t.Fatalf("expected exact detector label %q, got %v", tc.label, labels)
					}
				} else if evidence.DetectionFrames != 0 || len(labels) != 0 {
					t.Fatalf("unmatched detector result entered the model prompt: %v", labels)
				}
				response, err := json.Marshal(map[string]any{"output": []any{
					map[string]any{"type": "message", "content": []any{map[string]string{"type": "output_text", "text": track}}},
				}})
				return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(bytes.NewReader(response))}, err
			})
			compiler, sdk, plan := detectorTestCompiler(t)
			settings := DefaultSettings()
			s, err := New(t.Context(), t.TempDir(), settings, provider, provider, nl2dsl.Generator{Model: provider, Compiler: compiler}, zerolog.Nop())
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(s.Close)
			r := &run{dir: t.TempDir(), rows: map[int]section{}, changed: make(chan struct{}), prompt: string(archivedPrompt), live: &liveInput{}}
			meta := session{Source: "live", Vehicle: vehicle, Producer: producer, Settings: settings, PromptVersion: "v6", Plan: plan}
			s.watchLiveDetection(t.Context(), r, meta, liveDetectionSource{
				engine: func(_ context.Context, v, p string) (string, error) {
					if v != vehicle || p != producer {
						t.Fatalf("detector lookup used %q and %q", v, p)
					}
					return detector, nil
				},
				subscribe: func(_ context.Context, engine string, receive func(feed.Result)) {
					if engine != detector {
						t.Fatalf("subscribed to engine %q", engine)
					}
					if tc.label != "" {
						receive(feed.Result{Engine: engine, Vehicle: vehicle, Producer: tc.resultProducer,
							Type: "DetectionResult", Session: tc.streamSession, Seq: 7,
							JSON: []byte(`{"detections":[{"className":"` + tc.label + `","score":0.89}]}`)})
					}
				},
			})
			row := section{sectionSummary: sectionSummary{Index: 1, End: 1}, Evidence: r.live.evidence.snapshot(producer, job)}
			if err := s.process(r, meta, row, func() ([]sample, error) {
				return []sample{{Image: nl2dsl.Image{JPEG: frame.Bytes()}}}, nil
			}); err != nil {
				t.Fatal(err)
			}
			got := r.rows[1]
			if got.CompileStatus != "compiled" || got.Mission == nil || calls != 1 || len(sdk.builds) != 1 {
				t.Fatalf("compile status = %q, error = %q, model calls = %d, build count = %d", got.CompileStatus, got.Error, calls, len(sdk.builds))
			}
			build := sdk.builds[0]
			builtDSL, err := mission.AstToDSL(build.Ast)
			if err != nil {
				t.Fatal(err)
			}
			if tc.wantTrack {
				want := map[string]string{"Camera": "Camera_rear", "Engine": detector, "Class": target}
				nodes := got.Mission.Graph.Nodes
				if got.InferenceStatus != "valid" || len(build.Manifest) == 0 || len(nodes) != 2 || nodes[0].TypeName != "Track" {
					t.Fatalf("expected a bound Track, got %q (%s): %s", got.InferenceText, got.Adaptation, got.DSL)
				}
				for field, value := range want {
					if got := nodes[0].Params[field]; got != value {
						t.Fatalf("Track %s = %q, want %q", field, got, value)
					}
				}
				for _, value := range []string{"Camera=params.Camera_rear", "Engine=DetectionEngine_local_rear_detector", `Class="Inspection Marker: Red"`, "low_battery -> home"} {
					if !strings.Contains(builtDSL, value) {
						t.Fatalf("SDK build missing %q:\n%s", value, builtDSL)
					}
				}
			} else if got.InferenceText != wait || got.InferenceStatus != "fallback" || len(build.Flow) != 0 ||
				!strings.Contains(got.Adaptation, "more than one camera-to-detector route") ||
				strings.Contains(builtDSL, "actions.Track") || !strings.Contains(builtDSL, "actions.Wait waiting(Duration=30") {
				t.Fatalf("expected the timed fallback, got %q (%s):\n%s", got.InferenceText, got.Adaptation, builtDSL)
			}
		})
	}
}

type detectorTestTransport func(*http.Request) (*http.Response, error)

func (f detectorTestTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func detectorTestProvider(complete detectorTestTransport) nl2dsl.Provider {
	return nl2dsl.Provider{Name: "openai", ModelID: "test", Key: "fake-key", BaseURL: "https://model.example",
		Client: &http.Client{Transport: complete}}
}

type detectorSDKRecorder struct {
	registry map[string]any
	builds   []mission.BuildRequest
}

func (s *detectorSDKRecorder) Registry(context.Context, []byte) (map[string]any, error) {
	return s.registry, nil
}

func (s *detectorSDKRecorder) Build(_ context.Context, req mission.BuildRequest) ([]byte, error) {
	s.builds = append(s.builds, req)
	return []byte("sdk build fixture"), nil
}

const detectorTestIP = "100.64.0.1"

func detectorTestCompiler(t *testing.T) (*mission.Service, *detectorSDKRecorder, mission.PlanningContext) {
	t.Helper()
	raw, err := os.ReadFile("../mission/testdata/registry.json")
	if err != nil {
		t.Fatal(err)
	}
	sdk := &detectorSDKRecorder{}
	if err := json.Unmarshal(raw, &sdk.registry); err != nil {
		t.Fatal(err)
	}
	sdk.registry["actions"].(map[string]any)["actions.Track"] = map[string]any{
		"type": "actions.Track", "fields": []any{
			map[string]any{"name": "Camera", "type": "params.Camera"},
			map[string]any{"name": "Engine", "type": "params.DetectionEngine"},
			map[string]any{"name": "Class", "type": "string"},
		},
	}
	sdk.registry["enums"].(map[string]any)["params.Camera"] = map[string]any{
		"fields": []any{map[string]any{"name": "Camera_front"}, map[string]any{"name": "Camera_rear"}},
	}

	plugin, err := os.ReadFile("../mission/testdata/driver.plugin.toml")
	if err != nil {
		t.Fatal(err)
	}
	main, tail, ok := strings.Cut(string(plugin), "  [driver.supported]")
	if !ok {
		t.Fatal("fixture missing supported services")
	}
	_, camera, ok := strings.Cut(main, "  [[driver.cameras]]")
	if !ok {
		t.Fatal("fixture missing front camera")
	}
	camera = strings.NewReplacer(`name = "front"`, `name = "rear"`, "primary = true", "primary = false").Replace(camera)
	plugin = []byte(main + "  [[driver.cameras]]" + camera + "  [driver.supported]" + tail)
	streams, problems, err := flows.ParseDriver(plugin)
	if err != nil || len(problems) != 0 {
		t.Fatalf("fixture driver: %v, %v", problems, err)
	}
	vehicle := detectorTestIP + ".drone"
	daemon := flows.Daemon{IP: detectorTestIP, Local: true, Vehicles: []flows.Vehicle{{Name: vehicle, Streams: streams, Typed: true}}}
	flow := &flows.Template{Vehicle: vehicle}
	for _, stream := range streams {
		if stream.Kind != "camera" {
			continue
		}
		bare := stream.Name + "_detector"
		engine, producer := detectorTestIP+"."+bare, "drone."+stream.Name
		daemon.Engines = append(daemon.Engines, flows.Engine{Name: engine, Bare: bare, Typed: true,
			Accepts: []string{stream.Type}, Result: "steeleagle_protocol/v1/messages/result/DetectionResult"})
		flow.Producers = append(flow.Producers, flows.TemplateProducer{Name: producer})
		flow.Engines = append(flow.Engines, flows.TemplateEngine{Name: engine})
		flow.Edges = append(flow.Edges, flows.TemplateEdge{From: producer, To: engine, Timeout: "1s"})
	}
	cfg, err := mission.ResolveConfig(mission.CompilerConfig{SDKVersion: "v0.1.3"})
	if err != nil {
		t.Fatal(err)
	}
	compiler := mission.NewService(sdk, cfg, detectorTestIP, func(context.Context) (flows.Catalog, error) {
		return flows.Catalog{Daemons: []flows.Daemon{daemon}}, nil
	}, func(context.Context) ([]fleet.Vehicle, error) {
		return []fleet.Vehicle{{Name: vehicle, Config: plugin, Driver: &config.DriverCap{}}}, nil
	}, zerolog.Nop())
	return compiler, sdk, mission.PlanningContext{Vehicle: vehicle, Flow: flow}
}
