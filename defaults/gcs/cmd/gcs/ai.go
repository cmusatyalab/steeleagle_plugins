package main

import (
	"context"
	"encoding/json"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	framepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/frame"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/aiconfig"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/chat"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/feed"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/inferredmission"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/nl2dsl"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/rtc"
	"github.com/rs/zerolog"
)

func aiProvider(args aiconfig.Options) (nl2dsl.Provider, nl2dsl.Provider) {
	key, openAIBase := os.Getenv("OPENAI_API_KEY"), os.Getenv("OPENAI_BASE_URL")
	provider := strings.ToLower(defaultString(args.ChatProvider, "openai"))
	model := defaultString(args.ChatModel, "gpt-6.1-sol")
	chat := nl2dsl.Provider{Name: provider, ModelID: model, Key: key, BaseURL: openAIBase,
		Reasoning: reasoning(model, args.ChatReasoning), Temperature: args.ChatTemperature}
	if provider == "anthropic" {
		chat.ModelID = defaultString(args.ChatModel, "claude-sonnet-4-20250514")
		chat.Key, chat.BaseURL, chat.Reasoning = os.Getenv("ANTHROPIC_API_KEY"), os.Getenv("ANTHROPIC_BASE_URL"), ""
	}
	visionModel := defaultString(args.VisionModel, "gpt-6.1-sol")
	vision := nl2dsl.Provider{Name: "openai", ModelID: visionModel, Key: key,
		BaseURL: defaultString(os.Getenv("STEELEAGLE_VISION_BASE_URL"), openAIBase), Reasoning: reasoning(visionModel, args.VisionReasoning), Temperature: args.VisionTemperature}
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

func registerAI(ctx context.Context, a aiconfig.Options, missions *mission.Service, hub *rtc.Hub, log zerolog.Logger) (func(*http.ServeMux), *inferredmission.Service) {
	provider, vision := aiProvider(a)
	generator := nl2dsl.Generator{Model: provider, Compiler: missions}
	chatAPI := chat.New(provider, generator, chat.Credentials{
		OpenAI: vision.Key != "", Anthropic: os.Getenv("ANTHROPIC_API_KEY") != "",
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
		if relative := msg.Telemetry.GetPositionInfo().GetRelativePosition(); relative != nil {
			inference.Telemetry(msg.Vehicle, float64(relative.GetZ()), now)
		}
	}
	frame := msg.Frame
	if frame == nil || frame.GetCodec() != framepb.Codec_CODEC_H264 {
		return
	}
	encoded := inferredmission.EncodedFrame{Producer: msg.Producer, Vehicle: msg.Vehicle, Data: frame.GetEncodedData(),
		ReceivedAt: now, ID: frame.GetId(), Keyframe: frame.GetKeyframe()}
	if frame.HasTimestamp() && frame.GetTimestamp().IsValid() {
		encoded.CapturedAt = frame.GetTimestamp().AsTime()
	}
	if position := frame.GetPositionInfo().GetRelativePosition(); position != nil {
		meters := float64(position.GetZ())
		if !math.IsNaN(meters) && !math.IsInf(meters, 0) {
			encoded.RelativeAltitude = &meters
		}
	}
	inference.Ingest(encoded)
}
