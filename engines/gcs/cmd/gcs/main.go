// Command gcs is the SteelEagle ground control station. It runs as an engine
// plugin: dataflows send it frames and telemetry, which it streams to
// browsers over WebRTC, and it serves the web UI.
//
// The daemon passes the plugin's args as JSON in STEELEAGLE_PLUGIN_ARGS (see
// gcs.plugin.toml), the name of its Uriel engine in STEELEAGLE_ENGINE_ENDPOINT, and the
// socket the GCS calls it on, to control vehicles, in STEELEAGLE_CLIENT_SOCKET.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/control"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/feed"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/rtc"
	"github.com/rs/zerolog"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

const (
	argsEnv   = "STEELEAGLE_PLUGIN_ARGS"
	engineEnv = "STEELEAGLE_ENGINE_ENDPOINT"
	daemonEnv = "STEELEAGLE_CLIENT_SOCKET"
	// shutdownTimeout is how long requests may take to finish on shutdown.
	shutdownTimeout = 5 * time.Second
)

// args are the plugin's args.
type args struct {
	HTTPAddr     string   `json:"http_addr"`
	ICEServers   []string `json:"ice_servers"`
	WebRTCPorts  string   `json:"webrtc_ports"`
	MaxInputSize int      `json:"max_input_size"`
	// Compiler configures compiling missions; see mission.CompilerConfig.
	Compiler mission.CompilerConfig `json:"compiler"`
}

// parseArgs reads the plugin's args from JSON, defaulting the ones it leaves
// out as gcs.plugin.toml does.
func parseArgs(content string) (args, error) {
	a := args{HTTPAddr: ":8002", MaxInputSize: 4 << 20}
	if content != "" {
		if err := json.Unmarshal([]byte(content), &a); err != nil {
			return args{}, fmt.Errorf("invalid plugin args: %w", err)
		}
	}
	if a.MaxInputSize <= 0 {
		return args{}, fmt.Errorf("invalid plugin args: max_input_size must be positive, not %d", a.MaxInputSize)
	}
	return a, nil
}

// parsePorts reads a port range "min-max"; empty for any port.
func parsePorts(s string) (uint16, uint16, error) {
	if s == "" {
		return 0, 0, nil
	}
	lo, hi, ok := strings.Cut(s, "-")
	if !ok {
		return 0, 0, fmt.Errorf("invalid port range %q: want min-max", s)
	}
	min, err := strconv.ParseUint(lo, 10, 16)
	if err != nil {
		return 0, 0, fmt.Errorf("invalid port range %q: %w", s, err)
	}
	max, err := strconv.ParseUint(hi, 10, 16)
	if err != nil {
		return 0, 0, fmt.Errorf("invalid port range %q: %w", s, err)
	}
	if min == 0 || min > max {
		return 0, 0, fmt.Errorf("invalid port range %q", s)
	}
	return uint16(min), uint16(max), nil
}

func main() {
	web := flag.String("web", "web/dist", "directory of the built web UI")
	engine := flag.String("engine", os.Getenv(engineEnv), "name of the Uriel engine (default: $"+engineEnv+")")
	daemon := flag.String("daemon", os.Getenv(daemonEnv), "socket of the daemon (default: $"+daemonEnv+")")
	level := flag.String("log-level", "info", "log level")
	flag.Parse()

	log := zerolog.New(os.Stderr).With().Timestamp().Str("plugin", "gcs").Logger()
	if l, err := zerolog.ParseLevel(*level); err == nil {
		log = log.Level(l)
	} else {
		log.Fatal().Err(err).Msg("invalid log level")
	}
	if err := run(*engine, *daemon, *web, os.Getenv(argsEnv), log); err != nil {
		log.Fatal().Err(err).Msg("gcs failed")
	}
}

func run(engine, daemon, web, rawArgs string, log zerolog.Logger) error {
	if engine == "" {
		return fmt.Errorf("no engine name: set -engine or $%s", engineEnv)
	}
	a, err := parseArgs(rawArgs)
	if err != nil {
		return err
	}
	portMin, portMax, err := parsePorts(a.WebRTCPorts)
	if err != nil {
		return err
	}
	hub, err := rtc.NewHub(rtc.Options{ICEServers: a.ICEServers, PortMin: portMin, PortMax: portMax}, log)
	if err != nil {
		return err
	}
	defer hub.Close()

	var conn *grpc.ClientConn
	if daemon != "" {
		conn, err = grpc.NewClient("unix://"+daemon, grpc.WithTransportCredentials(insecure.NewCredentials()))
		if err != nil {
			return fmt.Errorf("could not connect to the daemon: %w", err)
		}
		defer conn.Close()
	} else {
		log.Warn().Msg("no daemon socket; vehicles can't be controlled")
	}
	controller := control.New(grpcConn(conn), control.DefaultTimeout, log)
	vehicles := noDaemonHandler()
	if conn != nil {
		flows := fleet.NewFlows(conn, fleet.FlowSpec{Engine: engine}, log)
		controller.OnHold(flows.PushAfterHold)
		vehicles = http.HandlerFunc(fleet.NewVehicles(conn, flows, log).ServeVehicles)
	}

	cfg, err := mission.ResolveConfig(a.Compiler)
	var compiler mission.Compiler
	if err != nil {
		log.Error().Err(err).Msg("invalid compiler config; missions can't be compiled")
		compiler = unavailableCompiler{err}
	} else {
		compiler = mission.NewSDKCompiler(cfg, 2)
	}
	missions := missionRoutes{svc: mission.NewService(compiler, cfg, log), upload: noDaemonHandler(), deploy: noDaemonHandler()}
	if conn != nil {
		list := func(ctx context.Context) ([]fleet.Vehicle, error) {
			snap, err := fleet.List(ctx, daemonpb.NewDaemonServiceClient(conn))
			return snap.Vehicles, err
		}
		uploads := mission.NewUploads(list, mission.NewSender(conn, 1<<20, 30*time.Second), missions.svc, log)
		missions.upload = http.HandlerFunc(uploads.ServeUpload)
		missions.deploy = http.HandlerFunc(uploads.ServeDeploy)
	}

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	ln, err := net.Listen("tcp", a.HTTPAddr)
	if err != nil {
		return err
	}
	server := &http.Server{Handler: newHandler(hub, controller, vehicles, missions, web, log), ReadHeaderTimeout: 10 * time.Second}
	errs := make(chan error, 2)
	go func() {
		log.Info().Str("addr", ln.Addr().String()).Msg("serving web UI")
		if err := server.Serve(ln); !errors.Is(err, http.ErrServerClosed) {
			errs <- fmt.Errorf("web server failed: %w", err)
		}
	}()
	go func() {
		if err := feed.Run(ctx, engine, a.MaxInputSize, log, hub.Publish); err != nil {
			errs <- err
		}
	}()

	select {
	case <-ctx.Done():
		log.Info().Msg("shutting down")
	case err = <-errs:
	}
	cancel()
	shutdownCtx, cancelShutdown := context.WithTimeout(context.Background(), shutdownTimeout)
	defer cancelShutdown()
	if shutdownErr := server.Shutdown(shutdownCtx); shutdownErr != nil {
		log.Warn().Err(shutdownErr).Msg("could not shut down web server")
	}
	return err
}

// grpcConn returns conn as a connection interface, which is nil if conn is.
func grpcConn(conn *grpc.ClientConn) grpc.ClientConnInterface {
	if conn == nil {
		return nil
	}
	return conn
}

// noDaemonHandler answers that the GCS can't reach a daemon.
func noDaemonHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusServiceUnavailable)
		fmt.Fprintln(w, `{"detail":"the GCS has no connection to the daemon"}`)
	})
}

// unavailableCompiler is the compiler when its config can't be resolved: it
// fails every call with that error, so the compiler routes say why.
type unavailableCompiler struct{ err error }

func (c unavailableCompiler) Registry(context.Context) (map[string]any, error) { return nil, c.err }

func (c unavailableCompiler) Build(context.Context, mission.BuildRequest) ([]byte, error) {
	return nil, c.err
}

// missionRoutes are what the mission routes are served by. upload and deploy
// need the daemon.
type missionRoutes struct {
	svc            *mission.Service
	upload, deploy http.Handler
}

// newHandler serves the API, and the web UI from the directory web. vehicles
// serves the vehicle list, and missions the mission routes.
func newHandler(hub *rtc.Hub, controller *control.Controller, vehicles http.Handler, missions missionRoutes, web string, log zerolog.Logger) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/webrtc/offer", hub.ServeOffer)
	mux.HandleFunc("GET /api/streams", hub.ServeStreams)
	mux.HandleFunc("POST /api/start", controller.ServeStart)
	mux.HandleFunc("POST /api/command", controller.ServeCommand)
	mux.HandleFunc("POST /api/joystick", controller.ServeJoystick)
	mux.HandleFunc("POST /api/gimbal", controller.ServeGimbal)
	mux.Handle("GET /api/remote/vehicles", vehicles)
	mux.HandleFunc("GET /api/schema", missions.svc.ServeSchema)
	mux.HandleFunc("POST /api/compile", missions.svc.ServeCompile)
	mux.HandleFunc("POST /api/render_dsl", missions.svc.ServeRenderDSL)
	mux.HandleFunc("POST /api/parse_dsl", missions.svc.ServeParseDSL)
	mux.HandleFunc("POST /api/build", missions.svc.ServeBuild)
	mux.Handle("POST /api/upload", missions.upload)
	mux.Handle("POST /api/deploy", missions.deploy)
	mux.HandleFunc("/api/", http.NotFound)
	if _, err := os.Stat(filepath.Join(web, "index.html")); err != nil {
		log.Warn().Err(err).Str("web", web).Msg("no web UI to serve; serving only the API")
		return mux
	}
	mux.Handle("/", spaHandler(web))
	return mux
}

// spaHandler serves the files of the single page app in dir, and its
// index.html for paths that aren't files, which the app routes itself.
func spaHandler(dir string) http.Handler {
	files := http.FileServer(http.Dir(dir))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		name := filepath.Join(dir, filepath.FromSlash(path.Clean("/"+r.URL.Path)))
		if info, err := os.Stat(name); err != nil || info.IsDir() {
			http.ServeFile(w, r, filepath.Join(dir, "index.html"))
			return
		}
		files.ServeHTTP(w, r)
	})
}
