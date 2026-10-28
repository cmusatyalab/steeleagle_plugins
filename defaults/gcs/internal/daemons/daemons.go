// Package daemons serves the Orchestrate page: every daemon the GCS's own
// daemon knows of, with the services each runs; restarting a service or a
// whole daemon; a daemon's config and packages. Everything goes through the
// GCS's daemon: a call for a particular daemon carries that daemon's IP as
// its route key, which the GCS's daemon routes to it (or to itself).
package daemons

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"sort"
	"strings"
	"time"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"github.com/rs/zerolog"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const (
	// listTimeout bounds the ListServices call.
	listTimeout = 10 * time.Second
	// restartTimeout bounds the RestartService call, which waits for the
	// service to start again.
	restartTimeout = 30 * time.Second
	// maxBody bounds the body of a restart request.
	maxBody = 4 << 10
	// readTimeout bounds the config and package reads.
	readTimeout = 10 * time.Second
	// writeTimeout bounds Configure, Restart and RemovePackage, which the
	// daemon answers before it restarts.
	writeTimeout = 30 * time.Second
	// installTimeout bounds InstallPackage, which downloads a repository.
	installTimeout = 10 * time.Minute

	routeHeader = "x-route-key"
	// failedServices is in the error of a daemon that is reachable but
	// couldn't start its services; it still accepts Configure.
	failedServices = "daemon failed to start services"
	// authHint follows a PermissionDenied from a daemon.
	authHint = ` (the GCS service needs auth = "admin" in this daemon's config)`
)

// Daemon states, in Daemon.State.
const (
	StateUp             = "up"
	StateServicesFailed = "services_failed"
	StateUnreachable    = "unreachable"
)

// Patterns are the mux patterns of every route Routes serves, so the server
// can answer each with 503 when it has no daemon connection.
var Patterns = []string{
	"GET /api/daemons",
	"POST /api/daemons/restart_service",
	"POST /api/daemons/restart",
	"GET /api/daemons/packages",
	"POST /api/daemons/packages/install",
	"POST /api/daemons/packages/remove",
	"GET /api/daemons/config",
	"POST /api/daemons/config/convert",
	"POST /api/daemons/config/check",
	"PUT /api/daemons/config",
}

// Service is a service a daemon runs. Name is its full service name, Bare
// the part after its daemon's IP.
type Service struct {
	Name     string `json:"name"`
	Bare     string `json:"bare"`
	Plugin   string `json:"plugin"`
	Category string `json:"category"`
	Running  bool   `json:"running"`
	Auth     string `json:"auth"`
	// Self is whether the service is this GCS, whose restart disconnects it.
	Self bool `json:"self"`
}

// Daemon is a daemon and its services. Error is why it can't be listed, in
// which case it has no services; State tells a daemon that couldn't start
// its services (it can still be configured) from one that can't be reached.
type Daemon struct {
	IP       string    `json:"ip"`
	Hostname string    `json:"hostname"`
	Local    bool      `json:"local"`
	Arch     string    `json:"arch"`
	OS       string    `json:"os"`
	State    string    `json:"state"`
	Error    string    `json:"error,omitempty"`
	Services []Service `json:"services"`
}

// Self is what the GCS knows of its own daemon, which ListServices doesn't
// list among the remotes. Arch and OS are the GCS process's, which runs on
// that daemon. Engine is the GCS's own full service name.
type Self struct {
	IP, Hostname, Arch, OS, Engine string
}

// client is the part of the daemon client the routes need.
type client interface {
	ListServices(ctx context.Context, in *daemonpb.ListServicesRequest, opts ...grpc.CallOption) (*daemonpb.ListServicesResponse, error)
	RestartService(ctx context.Context, in *daemonpb.RestartServiceRequest, opts ...grpc.CallOption) (*daemonpb.RestartServiceResponse, error)
	GetConfiguration(ctx context.Context, in *daemonpb.GetConfigurationRequest, opts ...grpc.CallOption) (*daemonpb.GetConfigurationResponse, error)
	Configure(ctx context.Context, in *daemonpb.ConfigureRequest, opts ...grpc.CallOption) (*daemonpb.ConfigureResponse, error)
	Restart(ctx context.Context, in *daemonpb.RestartRequest, opts ...grpc.CallOption) (*daemonpb.RestartResponse, error)
	ListPackages(ctx context.Context, in *daemonpb.ListPackagesRequest, opts ...grpc.CallOption) (*daemonpb.ListPackagesResponse, error)
	InstallPackage(ctx context.Context, in *daemonpb.InstallPackageRequest, opts ...grpc.CallOption) (*daemonpb.InstallPackageResponse, error)
	RemovePackage(ctx context.Context, in *daemonpb.RemovePackageRequest, opts ...grpc.CallOption) (*daemonpb.RemovePackageResponse, error)
}

// routed returns ctx with ip as its route key, so the GCS's daemon sends the
// call to the daemon at ip (itself included).
func routed(ctx context.Context, ip string) context.Context {
	return metadata.AppendToOutgoingContext(ctx, routeHeader, ip)
}

// state is a listed daemon's state, from the error it was listed with.
func state(err string) string {
	switch {
	case err == "":
		return StateUp
	case strings.Contains(err, failedServices):
		return StateServicesFailed
	default:
		return StateUnreachable
	}
}

// Build groups the services of resp by the daemon they run on: the GCS's
// own daemon first, then the others by IP.
func Build(resp *daemonpb.ListServicesResponse, self Self) []Daemon {
	byIP := map[string]*Daemon{self.IP: {IP: self.IP, Hostname: self.Hostname, Local: true, Arch: self.Arch, OS: self.OS}}
	for _, r := range resp.GetRemotes() {
		if r.GetIp() == self.IP {
			continue
		}
		byIP[r.GetIp()] = &Daemon{IP: r.GetIp(), Hostname: r.GetHostname(), Arch: r.GetArch(), OS: r.GetOs(), Error: r.GetError()}
	}
	for _, s := range resp.GetServices() {
		ip, bare := fleet.SplitName(s.GetName())
		if ip == "" {
			// The daemon qualifies every name it lists; skip any it didn't
			continue
		}
		d, ok := byIP[ip]
		if !ok {
			d = &Daemon{IP: ip}
			byIP[ip] = d
		}
		d.Services = append(d.Services, Service{
			Name: s.GetName(), Bare: bare, Plugin: s.GetPlugin(), Category: category(s.GetCategory()),
			Running: s.GetRunning(), Auth: s.GetAuth(), Self: s.GetName() == self.Engine,
		})
	}
	out := make([]Daemon, 0, len(byIP))
	for _, d := range byIP {
		d.State = state(d.Error)
		if d.Services == nil {
			d.Services = []Service{}
		}
		sort.Slice(d.Services, func(i, j int) bool { return d.Services[i].Name < d.Services[j].Name })
		out = append(out, *d)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Local != out[j].Local {
			return out[i].Local
		}
		return out[i].IP < out[j].IP
	})
	return out
}

// category is a service category's name: "vehicle", "engine", and so on.
func category(c daemonpb.ServiceCategory) string {
	return strings.ToLower(strings.TrimPrefix(c.String(), "SERVICE_CATEGORY_"))
}

// Handler serves the daemon routes.
type Handler struct {
	daemon client
	self   Self
	log    zerolog.Logger
}

// NewHandler returns the routes for the daemon c, which is the GCS's own
// daemon, self.
func NewHandler(c client, self Self, log zerolog.Logger) *Handler {
	return &Handler{daemon: c, self: self, log: log}
}

// Routes returns every route the handler serves, by mux pattern (Patterns).
func (h *Handler) Routes() map[string]http.Handler {
	return map[string]http.Handler{
		"GET /api/daemons":                   http.HandlerFunc(h.ServeList),
		"POST /api/daemons/restart_service":  http.HandlerFunc(h.ServeRestart),
		"POST /api/daemons/restart":          http.HandlerFunc(h.serveRestartDaemon),
		"GET /api/daemons/packages":          http.HandlerFunc(h.servePackages),
		"POST /api/daemons/packages/install": http.HandlerFunc(h.serveInstallPackage),
		"POST /api/daemons/packages/remove":  http.HandlerFunc(h.serveRemovePackage),
		"GET /api/daemons/config":            http.HandlerFunc(h.serveGetConfig),
		"POST /api/daemons/config/convert":   http.HandlerFunc(h.serveConvert),
		"POST /api/daemons/config/check":     http.HandlerFunc(h.serveCheck),
		"PUT /api/daemons/config":            http.HandlerFunc(h.serveApply),
	}
}

// ServeList serves GET /api/daemons: {"daemons": [...]}. It responds 502
// with a detail if the daemon can't be listed.
func (h *Handler) ServeList(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), listTimeout)
	defer cancel()
	resp, err := h.daemon.ListServices(ctx, &daemonpb.ListServicesRequest{})
	if err != nil {
		h.log.Warn().Err(err).Msg("list services for the daemons page")
		writeJSON(w, http.StatusBadGateway, map[string]string{"detail": status.Convert(err).Message()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"daemons": Build(resp, h.self)})
}

// ServeRestart serves POST /api/daemons/restart_service {"name": full service
// name}: {"ok": true}, or 502 with the daemon's reason as its detail.
func (h *Handler) ServeRestart(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Name string `json:"name"`
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBody))
	if err == nil {
		err = json.Unmarshal(body, &req)
	}
	if err != nil || req.Name == "" {
		detail := "a service name is required"
		if err != nil {
			detail = "invalid request: " + err.Error()
		}
		writeJSON(w, http.StatusBadRequest, map[string]string{"detail": detail})
		return
	}
	// A restart runs to the end even if the page goes away, so it never
	// leaves a service half restarted.
	ctx, cancel := context.WithTimeout(context.WithoutCancel(r.Context()), restartTimeout)
	defer cancel()
	if _, err := h.daemon.RestartService(ctx, daemonpb.RestartServiceRequest_builder{ServiceNames: req.Name}.Build()); err != nil {
		h.log.Warn().Err(err).Str("service", req.Name).Msg("restart service")
		writeJSON(w, http.StatusBadGateway, map[string]string{"detail": status.Convert(err).Message()})
		return
	}
	h.log.Info().Str("service", req.Name).Msg("restarted service")
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

// serveRestartDaemon serves POST /api/daemons/restart {"ip"}: {"ok": true}
// once the daemon has accepted the restart.
func (h *Handler) serveRestartDaemon(w http.ResponseWriter, r *http.Request) {
	var req struct {
		IP string `json:"ip"`
	}
	if !readJSON(w, r, maxBody, &req) {
		return
	}
	if req.IP == "" {
		writeDetail(w, http.StatusBadRequest, "a daemon ip is required")
		return
	}
	ctx, cancel := context.WithTimeout(routed(context.WithoutCancel(r.Context()), req.IP), writeTimeout)
	defer cancel()
	if _, err := h.daemon.Restart(ctx, &daemonpb.RestartRequest{}); err != nil {
		h.log.Warn().Err(err).Str("daemon", req.IP).Msg("restart daemon")
		writeRPCError(w, err)
		return
	}
	h.log.Info().Str("daemon", req.IP).Msg("restarting daemon")
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

// readJSON decodes a request body of at most limit bytes into v, answering
// 400 and returning false if it can't.
func readJSON(w http.ResponseWriter, r *http.Request, limit int64, v any) bool {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, limit))
	if err == nil {
		err = json.Unmarshal(body, v)
	}
	if err != nil {
		writeDetail(w, http.StatusBadRequest, "invalid request: "+err.Error())
		return false
	}
	return true
}

// writeRPCError answers with a daemon call's error: its own message, under
// the HTTP status closest to its gRPC code. A PermissionDenied says what the
// GCS needs.
func writeRPCError(w http.ResponseWriter, err error) {
	st := status.Convert(err)
	detail := st.Message()
	code := http.StatusBadGateway
	switch st.Code() {
	case codes.PermissionDenied:
		code, detail = http.StatusForbidden, detail+authHint
	case codes.NotFound:
		code = http.StatusNotFound
	case codes.InvalidArgument:
		code = http.StatusUnprocessableEntity
	case codes.FailedPrecondition:
		code = http.StatusConflict
	}
	writeDetail(w, code, detail)
}

func writeDetail(w http.ResponseWriter, code int, detail string) {
	writeJSON(w, code, map[string]string{"detail": detail})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
