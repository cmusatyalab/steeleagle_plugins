// Command gcsmock is a fake daemon for exercising the GCS without vehicles. It
// serves the daemon's gRPC API on a unix socket for simulated vehicles, hosts
// a Uriel server with their camera and telemetry producers and mock
// h264_encoder engines, and a mock detector on the GCS's daemon whose
// detections the GCS can draw on the video, and runs the real GCS binary as
// its child, as the daemon runs an engine plugin. It exits with the child's exit status.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	framepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/frame"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/feed"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"github.com/rs/zerolog"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	// tick is the period of the flight model.
	tick = 100 * time.Millisecond
	// cameraPeriod is 15 fps, the clips' rate.
	cameraPeriod = time.Second / clipFPS
	// telemetryPeriod is 10 Hz.
	telemetryPeriod = 100 * time.Millisecond
	// childStopTimeout is how long the child may take to stop after SIGINT.
	childStopTimeout = 10 * time.Second
	// grpcStopTimeout is how long gRPC calls may take to finish on shutdown.
	grpcStopTimeout = 2 * time.Second

	rawFrameMaxSize  = 4096
	telemetryMaxSize = 64 << 10
)

// cmu is where the vehicles start.
var cmu = LatLon{Lat: 40.4433, Lon: -79.9436}

// options are the command's flags.
type options struct {
	gcs, web, http, socket, clips string
	mapbox                        string
	gcsIP                         string
	vehicles                      []string
	noEncoder                     map[string]bool
	noYolo                        map[string]bool
	noDetector                    bool
	denyConfig                    map[string]bool
	archs                         map[string]string // daemon IP -> architecture
}

func main() {
	// The child is told to stop if the thread that started it exits; the
	// main goroutine's thread lasts as long as the process
	runtime.LockOSThread()

	log := zerolog.New(zerolog.ConsoleWriter{Out: os.Stderr, TimeFormat: time.TimeOnly}).
		With().Timestamp().Str("cmd", "gcsmock").Logger()
	opts, err := parseFlags(os.Args[1:])
	if err != nil {
		log.Fatal().Err(err).Msg("invalid flags")
	}
	code, err := run(opts, log)
	if err != nil {
		log.Error().Err(err).Msg("gcsmock failed")
		if code == 0 {
			code = 1
		}
	}
	os.Exit(code)
}

func parseFlags(args []string) (options, error) {
	fs := flag.NewFlagSet("gcsmock", flag.ContinueOnError)
	runtimeDir := os.Getenv("XDG_RUNTIME_DIR")
	if runtimeDir == "" {
		runtimeDir = os.TempDir()
	}
	var o options
	fs.StringVar(&o.gcs, "gcs", "bin/gcs", "path of the GCS binary")
	fs.StringVar(&o.web, "web", "web/dist", "directory of the built web UI, passed to the GCS")
	fs.StringVar(&o.http, "http", ":8002", "address the GCS serves the web UI on")
	fs.StringVar(&o.socket, "socket", filepath.Join(runtimeDir, fmt.Sprintf("gcsmock-%d.sock", os.Getpid())),
		"unix socket the mock daemon serves on")
	fs.StringVar(&o.clips, "clips", "bin/mockclips", "directory the video clips are cached in")
	fs.StringVar(&o.mapbox, "mapbox", os.Getenv("MAPBOX_TOKEN"), "Mapbox access token passed to the GCS (default: $MAPBOX_TOKEN)")
	fs.StringVar(&o.gcsIP, "gcs-ip", "100.64.0.1", "IP of the daemon the GCS runs on")
	vehicles := fs.String("vehicles", "100.64.0.1.alpha,100.64.0.1.bravo,100.64.0.2.charlie",
		"comma-separated vehicle names, each its daemon's IP, a dot and its own name")
	noEncoder := fs.String("no-encoder-daemons", "", "comma-separated IPs of daemons without an h264_encoder")
	noYolo := fs.String("no-yolo-daemons", "", "comma-separated IPs of daemons without a yolo engine")
	fs.BoolVar(&o.noDetector, "no-detector", false, "don't run the mock detector on the GCS's daemon")
	denyConfig := fs.String("deny-config-daemons", "", "comma-separated IPs of daemons that refuse config, restart and package changes with PermissionDenied")
	archs := fs.String("daemon-arch", "",
		"comma-separated ip=arch entries (amd64 or arm64) for daemon architectures; others are amd64")
	if err := fs.Parse(args); err != nil {
		return options{}, err
	}
	if fs.NArg() > 0 {
		return options{}, fmt.Errorf("unexpected arguments %q", fs.Args())
	}
	o.vehicles = splitList(*vehicles)
	if len(o.vehicles) == 0 {
		return options{}, errors.New("no vehicles")
	}
	seen := map[string]bool{}
	for _, v := range o.vehicles {
		if ip, bare := fleet.SplitName(v); ip == "" || bare == "" || strings.Contains(bare, ".") {
			return options{}, fmt.Errorf("vehicle %q isn't a daemon IP, a dot and a name", v)
		}
		if seen[v] {
			return options{}, fmt.Errorf("vehicle %q is listed twice", v)
		}
		seen[v] = true
	}
	o.noEncoder = map[string]bool{}
	for _, ip := range splitList(*noEncoder) {
		o.noEncoder[ip] = true
	}
	o.denyConfig = map[string]bool{}
	for _, ip := range splitList(*denyConfig) {
		o.denyConfig[ip] = true
	}
	o.noYolo = map[string]bool{}
	for _, ip := range splitList(*noYolo) {
		o.noYolo[ip] = true
	}
	o.archs = map[string]string{}
	for _, e := range splitList(*archs) {
		ip, arch, ok := strings.Cut(e, "=")
		if !ok || ip == "" || (arch != "amd64" && arch != "arm64") {
			return options{}, fmt.Errorf("daemon-arch entry %q isn't ip=amd64 or ip=arm64", e)
		}
		o.archs[ip] = arch
	}
	return o, nil
}

func splitList(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// topology returns the names the daemon lists: an encoder and a yolo engine
// on each vehicle daemon that isn't excluded, and the detector on the GCS's
// daemon unless it is.
func (o options) topology() Topology {
	top := Topology{GCSIP: o.gcsIP, Vehicles: o.vehicles, EncoderDaemons: map[string]bool{}, Archs: o.archs, YoloDaemons: map[string]bool{}, DenyConfig: o.denyConfig}
	if !o.noDetector {
		top.Detector = o.gcsIP + "." + detectorService
	}
	for _, ip := range o.daemonIPs() {
		if !o.noEncoder[ip] {
			top.EncoderDaemons[ip] = true
		}
		if !o.noYolo[ip] {
			top.YoloDaemons[ip] = true
		}
	}
	return top
}

// daemonIPs returns the IPs of the vehicles' daemons, in order.
func (o options) daemonIPs() []string {
	var ips []string
	seen := map[string]bool{}
	for _, v := range o.vehicles {
		ip, _ := fleet.SplitName(v)
		if !seen[ip] {
			seen[ip] = true
			ips = append(ips, ip)
		}
	}
	return ips
}

// run runs the mock until the child exits or a signal stops it, and returns
// the child's exit status.
func run(o options, log zerolog.Logger) (int, error) {
	top := o.topology()
	// A signal before the child starts still shuts down cleanly: Uriel's
	// shared memory is released by Close
	signals := make(chan os.Signal, 2)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(signals)
	stopped := func() bool {
		select {
		case sig := <-signals:
			log.Info().Stringer("signal", sig).Msg("stopping before the GCS started")
			return true
		default:
			return false
		}
	}

	clips := map[string][]AccessUnit{}
	for _, v := range o.vehicles {
		units, err := loadClip(o.clips, v)
		if err != nil {
			return 1, err
		}
		clips[v] = units
		if stopped() {
			return 0, nil
		}
	}

	// The Uriel server and its engines, then the vehicles, whose flows the
	// GCS may push as soon as it starts
	u, err := NewUriel(fmt.Sprintf("gcsmock-%d", os.Getpid()), log)
	if err != nil {
		return 1, err
	}
	defer u.Close()
	for _, ip := range o.daemonIPs() {
		if top.EncoderDaemons[ip] {
			if err := u.StartEngine(ip+"."+encoderService, "", encoderMaxOutput, Encoder(clips)); err != nil {
				return 1, err
			}
		}
		if err := u.StartEngine(ip+".mission_sink", "", sinkMaxOutput, Sink); err != nil {
			return 1, err
		}
	}
	vehicles := map[string]*Vehicle{}
	for i, name := range o.vehicles {
		v := NewVehicle(name, offsetFrom(cmu, 0, float64(i)*15))
		vehicles[name] = v
		if err := startVehicle(u, name, v); err != nil {
			return 1, err
		}
	}
	if top.Detector != "" {
		if err := u.StartEngine(top.Detector, detectionID, detectorMaxOutput, Detector); err != nil {
			return 1, err
		}
	}
	gcsEngine := o.gcsIP + ".gcs"
	if err := u.ServeEngine(gcsEngine); err != nil {
		return 1, err
	}

	// The daemon's API
	if err := removeStaleSocket(o.socket); err != nil {
		return 1, err
	}
	ln, err := net.Listen("unix", o.socket)
	if err != nil {
		return 1, err
	}
	defer os.Remove(o.socket)
	grpcServer := grpc.NewServer()
	NewDaemon(top, vehicles, u, log).Register(grpcServer)
	grpcDone := make(chan struct{})
	go func() {
		defer close(grpcDone)
		if err := grpcServer.Serve(ln); err != nil {
			log.Error().Err(err).Msg("gRPC server failed")
		}
	}()
	// Once the child is gone: Uriel, then the daemon's API
	defer func() {
		u.Close()
		stopGRPC(grpcServer)
		<-grpcDone
	}()

	// The flight model
	simCtx, stopSim := context.WithCancel(context.Background())
	simDone := make(chan struct{})
	go func() {
		defer close(simDone)
		simulate(simCtx, vehicles)
	}()
	defer func() {
		stopSim()
		<-simDone
	}()

	log.Info().Str("socket", o.socket).Strs("vehicles", o.vehicles).Str("engine", gcsEngine).Msg("mock daemon up; starting the GCS")
	if stopped() {
		return 0, nil
	}
	return runChild(o, gcsEngine, signals, log)
}

// loadClip reads the vehicle's clip, generating it first if needed, and
// splits it into access units.
func loadClip(dir, vehicle string) ([]AccessUnit, error) {
	path, err := EnsureClip(dir, vehicle)
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	units, err := SplitAccessUnits(data)
	if err != nil {
		return nil, fmt.Errorf("could not read the clip %s: %w", path, err)
	}
	if !units[0].Key {
		return nil, fmt.Errorf("the clip %s doesn't start with a keyframe", path)
	}
	return units, nil
}

// startVehicle creates the vehicle's client and its camera and telemetry
// producers.
func startVehicle(u *Uriel, name string, v *Vehicle) error {
	if err := u.AddVehicle(name, "front", "telemetry"); err != nil {
		return err
	}
	var id atomic.Uint64
	err := u.StartProducer(name+".front", RawFrameType, rawFrameMaxSize, cameraPeriod, func(now time.Time) ([]byte, error) {
		return proto.Marshal(framepb.RawFrame_builder{
			Timestamp: timestamppb.New(now),
			Id:        id.Add(1),
			HRes:      1280,
			VRes:      720,
			Channels:  3,
		}.Build())
	})
	if err != nil {
		return err
	}
	return u.StartProducer(name+".telemetry", feed.TelemetryType, telemetryMaxSize, telemetryPeriod,
		func(now time.Time) ([]byte, error) { return proto.Marshal(v.Telemetry(now)) })
}

// removeStaleSocket removes a socket left at path, which nothing listens on.
func removeStaleSocket(path string) error {
	info, err := os.Stat(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if info.Mode().Type() != os.ModeSocket {
		return fmt.Errorf("%s exists and isn't a socket", path)
	}
	if conn, err := net.Dial("unix", path); err == nil {
		conn.Close()
		return fmt.Errorf("something already listens on %s", path)
	}
	return os.Remove(path)
}

// stopGRPC stops the server, cutting off calls that don't finish in time.
func stopGRPC(s *grpc.Server) {
	done := make(chan struct{})
	go func() {
		s.GracefulStop()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(grpcStopTimeout):
		s.Stop()
		<-done
	}
}

// simulate steps the vehicles every tick until ctx is done.
func simulate(ctx context.Context, vehicles map[string]*Vehicle) {
	ticker := time.NewTicker(tick)
	defer ticker.Stop()
	last := time.Now()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			for _, v := range vehicles {
				v.Step(now.Sub(last), now)
			}
			last = now
		}
	}
}

// runChild runs the GCS until it exits, stopping it on a signal from
// signals, and returns its exit status.
func runChild(o options, engine string, signals <-chan os.Signal, log zerolog.Logger) (int, error) {
	pluginArgs, err := json.Marshal(map[string]string{"http_addr": o.http, "mapbox": o.mapbox})
	if err != nil {
		return 1, err
	}
	cmd := exec.Command(o.gcs, "-web", o.web)
	cmd.Env = append(os.Environ(),
		"STEELEAGLE_ENGINE_ENDPOINT="+engine,
		"STEELEAGLE_CLIENT_SOCKET="+o.socket,
		"STEELEAGLE_PLUGIN_ARGS="+string(pluginArgs))
	stdout, stderr := newPrefixWriter(os.Stdout, "[gcs] "), newPrefixWriter(os.Stderr, "[gcs] ")
	cmd.Stdout, cmd.Stderr = stdout, stderr
	// The child gets signals only from the mock, which stops it first; it
	// stops too if the mock dies
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGTERM}

	if err := cmd.Start(); err != nil {
		return 1, fmt.Errorf("could not start the GCS %s: %w", o.gcs, err)
	}
	exited := make(chan error, 1)
	go func() { exited <- cmd.Wait() }()

	var waitErr error
	select {
	case waitErr = <-exited:
		log.Info().Msg("the GCS exited")
	case sig := <-signals:
		log.Info().Stringer("signal", sig).Msg("stopping the GCS")
		_ = cmd.Process.Signal(os.Interrupt)
		select {
		case waitErr = <-exited:
		case <-time.After(childStopTimeout):
			log.Warn().Msg("the GCS didn't stop in time; killing it")
			_ = cmd.Process.Kill()
			waitErr = <-exited
		}
	}
	stdout.Flush()
	stderr.Flush()
	return exitCode(waitErr)
}

// exitCode returns the exit status of a process that Wait returned err for:
// 128 plus the signal for a process a signal killed, as shells report it.
func exitCode(err error) (int, error) {
	if err == nil {
		return 0, nil
	}
	var exitErr *exec.ExitError
	if !errors.As(err, &exitErr) {
		return 1, err
	}
	if ws, ok := exitErr.Sys().(syscall.WaitStatus); ok && ws.Signaled() {
		return 128 + int(ws.Signal()), nil
	}
	return exitErr.ExitCode(), nil
}

// prefixWriter writes each line written to it to out, after a prefix.
type prefixWriter struct {
	mu      sync.Mutex
	out     io.Writer
	prefix  string
	partial []byte
}

func newPrefixWriter(out io.Writer, prefix string) *prefixWriter {
	return &prefixWriter{out: out, prefix: prefix}
}

func (w *prefixWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.partial = append(w.partial, p...)
	var buf bytes.Buffer
	for {
		i := bytes.IndexByte(w.partial, '\n')
		if i < 0 {
			break
		}
		buf.WriteString(w.prefix)
		buf.Write(w.partial[:i+1])
		w.partial = w.partial[i+1:]
	}
	if buf.Len() > 0 {
		if _, err := w.out.Write(buf.Bytes()); err != nil {
			return 0, err
		}
	}
	return len(p), nil
}

// Flush writes a last line that has no newline.
func (w *prefixWriter) Flush() {
	w.mu.Lock()
	defer w.mu.Unlock()
	if len(w.partial) > 0 {
		fmt.Fprintf(w.out, "%s%s\n", w.prefix, w.partial)
		w.partial = nil
	}
}
