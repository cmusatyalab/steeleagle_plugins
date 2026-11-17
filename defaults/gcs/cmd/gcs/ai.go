package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	framepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/frame"
	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/aiconfig"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/aicredentials"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/chat"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/feed"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/flows"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/inferredmission"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/nl2dsl"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/rtc"
	"github.com/rs/zerolog"
)

func aiProvider(args aiconfig.Options, credentials *aicredentials.Store) (nl2dsl.Provider, nl2dsl.Provider) {
	openAIBase := os.Getenv("OPENAI_BASE_URL")
	provider := strings.ToLower(defaultString(args.ChatProvider, "openai"))
	model := defaultString(args.ChatModel, "gpt-6.1-sol")
	chat := nl2dsl.Provider{Name: provider, ModelID: model, BaseURL: openAIBase,
		LookupKey: func() string { return credentials.Key(provider) },
		Reasoning: reasoning(model, args.ChatReasoning), Temperature: args.ChatTemperature}
	if provider == "anthropic" {
		chat.ModelID = defaultString(args.ChatModel, "claude-sonnet-4-20250514")
		chat.BaseURL, chat.Reasoning = os.Getenv("ANTHROPIC_BASE_URL"), ""
	}
	visionModel := defaultString(args.VisionModel, "gpt-6.1-sol")
	vision := nl2dsl.Provider{Name: "openai", ModelID: visionModel,
		LookupKey: func() string { return credentials.Key("openai") },
		BaseURL:   defaultString(os.Getenv("STEELEAGLE_VISION_BASE_URL"), openAIBase), Reasoning: reasoning(visionModel, args.VisionReasoning), Temperature: args.VisionTemperature}
	return chat, vision
}

func reasoning(model, configured string) string {
	if configured == "" && strings.HasPrefix(model, "gpt-6.1-sol") {
		return "medium"
	}
	return configured
}

func defaultString(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}

func openAICredentials(engine string) (*aicredentials.Store, error) {
	root, err := os.UserConfigDir()
	if err != nil {
		return nil, err
	}
	// The service name is stable if the daemon moves to another IP address.
	_, service := fleet.SplitName(engine)
	path := filepath.Join(root, "steeleagle", "gcs", "credentials", url.PathEscape(service)+".json")
	return aicredentials.Open(path)
}

func registerAI(ctx context.Context, engine string, a aiconfig.Options, missions *mission.Service, hub *rtc.Hub, log zerolog.Logger) (func(*http.ServeMux), *inferredmission.Service) {
	credentials, err := openAICredentials(engine)
	if err != nil {
		log.Warn().Err(err).Msg("saved AI credentials unavailable; using process environment")
		credentials, _ = aicredentials.Open("")
	}
	provider, vision := aiProvider(a, credentials)
	generator := nl2dsl.Generator{Model: provider, Compiler: missions}
	chatAPI := chat.New(provider, generator, func() chat.Credentials {
		return chat.Credentials{OpenAI: credentials.Key("openai") != "", Anthropic: credentials.Key("anthropic") != ""}
	})
	dataDir := defaultString(os.Getenv("STEELEAGLE_INFERRED_MISSION_DATA"), a.DataDir)
	var configErr error
	if dataDir == "" {
		var root string
		root, configErr = os.UserConfigDir()
		if configErr == nil {
			dataDir = filepath.Join(root, "steeleagle", "gcs", "inferred-mission")
		}
	}
	var inference *inferredmission.Service
	if configErr == nil {
		inference, configErr = inferredmission.New(ctx, dataDir, a.InferredMission, vision, provider, generator, log)
	}
	if configErr != nil {
		log.Warn().Err(configErr).Msg("inferred mission unavailable")
	}
	return func(mux *http.ServeMux) {
		credentials.Register(mux)
		chatAPI.Register(mux)
		if inference == nil {
			mux.HandleFunc("/api/inferred-mission/", func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusServiceUnavailable)
				_ = json.NewEncoder(w).Encode(map[string]string{"detail": "Inferred Mission is unavailable. Check the GCS data directory and settings."})
			})
			return
		}
		inference.Register(mux, func() []inferredmission.Source {
			var sources []inferredmission.Source
			for _, stream := range hub.Streams() {
				if stream.Kind == "video" && time.Since(stream.LastSeen) < 30*time.Second {
					sources = append(sources, inferredmission.Source{Producer: stream.Producer, Vehicle: stream.Vehicle})
				}
			}
			return sources
		})
	}, inference
}

func observeFeed(inference *inferredmission.Service, msg feed.Message) {
	now := time.Now()
	if msg.Telemetry != nil {
		inference.IngestTelemetry(msg.Vehicle, msg.Telemetry, now)
	}
	frame := msg.Frame
	if frame == nil || frame.GetCodec() != framepb.Codec_CODEC_H264 {
		return
	}
	encoded := inferredmission.EncodedFrame{Producer: msg.Producer, Vehicle: msg.Vehicle, Data: frame.GetEncodedData(),
		ReceivedAt: now, ID: frame.GetId(), Keyframe: frame.GetKeyframe(), Session: msg.Session, Seq: msg.Seq,
		Motion: inferredmission.CameraMotion(frame.GetPositionInfo(), frame.GetGimbalInfo())}
	if frame.HasTimestamp() && frame.GetTimestamp().IsValid() {
		encoded.CapturedAt = frame.GetTimestamp().AsTime()
	}
	inference.Ingest(encoded)
}

// liveDetector selects one detector on the GCS's daemon that can observe the
// requested producer in the vehicle's active flow. A full mission binding is
// validated separately against Plan's draft flow.
func liveDetector(cat flows.Catalog, flow *config.FlowConfig, gcsIP, vehicle, producer string) string {
	vehicleIP, _ := fleet.SplitName(vehicle)
	qualify := func(name string) string {
		return fleet.QualifyFlowName(name, vehicle, vehicleIP, gcsIP)
	}
	from := ""
	for _, node := range flow.Producers {
		// Older daemons use a bare vehicle stream in active flows. A named
		// engine producer still needs its own qualified identity.
		bareStream := !strings.ContainsAny(node.Name, "./") && vehicle+"."+node.Name == producer
		if qualify(node.Name) == producer || bareStream {
			from = node.Name
			break
		}
	}
	if from == "" || feed.VehicleOf(producer) != vehicle {
		return ""
	}
	local, ok := cat.Daemon(gcsIP)
	if !ok || !local.Local {
		return ""
	}
	detectors := map[string]bool{}
	for _, engine := range local.Engines {
		kind, _ := feed.ResultType(engine.Result)
		// Some other engines also emit DetectionResult, for example as a
		// tile relevance result. Include only declared object detectors.
		if engine.Typed && engine.Category != nil && *engine.Category == "detection" && kind == "DetectionResult" {
			detectors[engine.Name] = true
		}
	}
	edges := map[string][]string{}
	for _, edge := range flow.Edges {
		edges[edge.From] = append(edges[edge.From], edge.To)
	}
	pending := []string{from}
	seen := map[string]bool{}
	selected := ""
	for len(pending) != 0 {
		next := pending[0]
		pending = pending[1:]
		if seen[next] {
			continue
		}
		seen[next] = true
		if name := qualify(next); detectors[name] {
			if selected != "" && selected != name {
				return ""
			}
			selected = name
		}
		pending = append(pending, edges[next]...)
	}
	return selected
}
