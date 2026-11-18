package main

// The inferred_mission_headless plugin: Inferred Mission without the web UI.
// It observes one vehicle camera as a live run from its first frame,
// planned with a flow file, and logs each section's mission; with
// auto_deploy, it sends each to the vehicle.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	framepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/frame"
	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/aiconfig"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/feed"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/flows"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/inferredmission"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/nl2dsl"
	"github.com/rs/zerolog"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

const (
	// pluginEnv names the plugin file the daemon started (e.g.
	// gcs.plugin.toml); the GCS package runs one binary for each
	pluginEnv      = "STEELEAGLE_PLUGIN"
	headlessPlugin = "inferred_mission_headless"
	// How long to wait before trying to start a run again
	headlessRetry = 10 * time.Second
)

// isHeadless reports whether the daemon started the headless plugin.
func isHeadless() bool {
	return strings.TrimSuffix(filepath.Base(os.Getenv(pluginEnv)), ".plugin.toml") == headlessPlugin
}

type headlessArgs struct {
	// Vehicle is the vehicle's service name (e.g. 128.2.208.47.drone), or
	// its bare name (drone); empty observes the first vehicle that streams
	Vehicle string `json:"vehicle"`
	// Camera is the vehicle's camera stream (e.g. front); empty observes its
	// first that streams
	Camera string `json:"camera"`
	// Flow is the planning flow's TOML file, names relative to the vehicle
	// (vehicle.front, local.box_rerank), absolute or relative to the files
	// directory; Track needs one connecting the Track detector to the camera
	Flow string `json:"flow"`
	// GeoJSON is the map file Patrol and Survey use, as Flow; optional
	GeoJSON string `json:"geojson"`

	Detector        string `json:"detector"`
	LocalDetector   string `json:"local_detector"`
	Verifier        string `json:"verifier"`
	VerifierLiveDir string `json:"verifier_live_dir"`
	AutoDeploy      bool   `json:"auto_deploy"`

	MaxInputSize int                    `json:"max_input_size"`
	Compiler     mission.CompilerConfig `json:"compiler"`
	AI           aiconfig.Options       `json:"ai"`
}

func parseHeadlessArgs(content string) (headlessArgs, error) {
	a := headlessArgs{MaxInputSize: defaultMaxInputSize, AI: aiconfig.Defaults()}
	if content != "" {
		dec := json.NewDecoder(strings.NewReader(content))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&a); err != nil {
			return headlessArgs{}, fmt.Errorf("invalid plugin args: %w", err)
		}
	}
	if a.MaxInputSize <= 0 {
		return headlessArgs{}, fmt.Errorf("invalid plugin args: max_input_size must be positive, not %d", a.MaxInputSize)
	}
	settings := &a.AI.InferredMission
	// The plugin's own args, over the same settings in ai.inferred_mission
	for _, arg := range []struct {
		value string
		field *string
	}{{a.Detector, &settings.Detector}, {a.LocalDetector, &settings.LocalDetector},
		{a.Verifier, &settings.Verifier}, {a.VerifierLiveDir, &settings.VerifierLiveDir}} {
		if arg.value != "" {
			*arg.field = arg.value
		}
	}
	settings.AutoDeploy = settings.AutoDeploy || a.AutoDeploy
	if err := settings.Validate(); err != nil {
		return headlessArgs{}, fmt.Errorf("invalid plugin args: %w", err)
	}
	return a, nil
}

// filesPath is path, or if relative, path in the daemon's files directory.
func filesPath(path string) string {
	if path == "" || filepath.IsAbs(path) {
		return path
	}
	return filepath.Join(os.Getenv(filesEnv), path)
}

// camera reports whether producer is the camera a names, given
// vehicle (the frame's vehicle).
func (a headlessArgs) camera(vehicle, producer string) bool {
	if a.Vehicle != "" && vehicle != a.Vehicle {
		if _, bare := fleet.SplitName(vehicle); bare != a.Vehicle {
			return false
		}
	}
	return a.Camera == "" || producer == vehicle+"."+a.Camera
}

func runHeadless(engine, daemon, rawArgs string, log zerolog.Logger) error {
	if engine == "" {
		return fmt.Errorf("no engine name: set -engine or $%s", engineEnv)
	}
	if daemon == "" {
		return fmt.Errorf("no daemon socket: set -daemon or $%s", daemonEnv)
	}
	a, err := parseHeadlessArgs(rawArgs)
	if err != nil {
		return err
	}
	key, keyPath, err := readSecret(openAIKeyFile)
	if err != nil {
		return fmt.Errorf("no OpenAI key: %w", err)
	}
	if key == "" {
		return fmt.Errorf("no OpenAI key: put it in %s (a file in the daemon's files directory, kept out of configs and git)", keyPath)
	}
	var plan mission.PlanningContext
	if a.Flow != "" {
		data, err := os.ReadFile(filesPath(a.Flow))
		if err != nil {
			return fmt.Errorf("could not read the planning flow: %w", err)
		}
		flow, err := flows.ParseTemplateTOML(data)
		if err != nil {
			return fmt.Errorf("invalid planning flow %s: %w", a.Flow, err)
		}
		plan.Flow = &flow
	} else {
		log.Warn().Msg("no planning flow: Track missions can't be bound to a detector")
	}
	if a.GeoJSON != "" {
		data, err := os.ReadFile(filesPath(a.GeoJSON))
		if err != nil {
			return fmt.Errorf("could not read the map: %w", err)
		}
		plan.GeoJSON = string(data)
	}

	conn, err := grpc.NewClient("unix://"+daemon, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return fmt.Errorf("could not connect to the daemon: %w", err)
	}
	defer conn.Close()
	daemonClient := fleet.NewCachedDaemon(daemonpb.NewDaemonServiceClient(conn), listCacheTTL, listTimeout)
	cfg, err := mission.ResolveConfig(a.Compiler)
	if err != nil {
		return fmt.Errorf("invalid compiler config: %w", err)
	}
	gcsIP, _ := fleet.SplitName(engine)
	list := daemonLister(daemonClient, gcsIP, daemonHostname(context.Background(), daemonClient, log))
	catalog := func(ctx context.Context) (flows.Catalog, error) {
		_, cat, err := list(ctx)
		return cat, err
	}
	running := func(ctx context.Context) ([]fleet.Vehicle, error) {
		vs, _, err := list(ctx)
		return vs, err
	}
	missions := mission.NewService(mission.NewSDKCompiler(cfg, 2), cfg, gcsIP, catalog, running, log)
	flowOf := func(ctx context.Context, vehicle string) ([]byte, error) { return fleet.GetFlow(ctx, conn, vehicle) }

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	lookup := func(provider string) string {
		if provider == "openai" {
			return key
		}
		return ""
	}
	chat, vision := aiProvider(a.AI, lookup)
	dataDir := defaultString(os.Getenv("STEELEAGLE_INFERRED_MISSION_DATA"), a.AI.DataDir)
	if dataDir == "" {
		dataDir = filesPath(headlessPlugin)
	}
	if err := os.MkdirAll(dataDir, 0o700); err != nil {
		return err
	}
	inference, err := inferredmission.New(ctx, dataDir, a.AI.InferredMission, vision, chat,
		nl2dsl.Generator{Model: chat, Compiler: missions}, log)
	if err != nil {
		return err
	}
	defer inference.Close()
	results := func(ctx context.Context, name string, handle func(feed.Result)) {
		feed.SubscribeResults(ctx, name, log, handle)
	}
	wireInference(inference, conn, catalog, flowOf, missions, gcsIP, results)
	settings := a.AI.InferredMission
	log.Info().Str("vehicle", a.Vehicle).Str("camera", a.Camera).Str("flow", a.Flow).Str("detector", settings.Detector).
		Str("local_detector", settings.LocalDetector).Str("verifier", settings.Verifier).Bool("auto_deploy", settings.AutoDeploy).
		Str("data", dataDir).Msg("inferred mission headless: waiting for the camera's frames")

	// The run starts at the camera's first frame; if it can't, it is tried
	// again at a later one
	var mu sync.Mutex
	started, nextTry := false, time.Time{}
	start := func(vehicle, producer string) {
		mu.Lock()
		defer mu.Unlock()
		if started || time.Now().Before(nextTry) {
			return
		}
		p := plan
		p.Vehicle = vehicle
		if p.Flow != nil {
			flow := *p.Flow
			flow.Vehicle = vehicle
			p.Flow = &flow
		}
		id, err := inference.StartLive(vehicle, producer, p)
		if err != nil {
			nextTry = time.Now().Add(headlessRetry)
			log.Error().Err(err).Str("producer", producer).Msg("could not start observing; trying again")
			return
		}
		started = true
		log.Info().Str("producer", producer).Str("run", id).Msg("observing")
	}
	publish := func(msg feed.Message) {
		if frame := msg.Frame; frame != nil && frame.GetCodec() == framepb.Codec_CODEC_H264 && a.camera(msg.Vehicle, msg.Producer) {
			start(msg.Vehicle, msg.Producer)
		}
		observeFeed(inference, msg)
	}
	err = feed.Run(ctx, engine, a.MaxInputSize, log, publish)
	if ctx.Err() != nil {
		log.Info().Msg("shutting down")
		return nil
	}
	if err == nil {
		err = errors.New("the feed ended")
	}
	return err
}
