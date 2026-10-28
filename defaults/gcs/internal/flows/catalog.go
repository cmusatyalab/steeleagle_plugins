package flows

import (
	"context"
	"encoding/json"
	"net/http"
	"sort"
	"strings"
	"time"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"github.com/rs/zerolog"
	"google.golang.org/grpc"
)

// listTimeout bounds the ListServices call.
const listTimeout = 10 * time.Second

// Vehicle is a vehicle service and the streams it produces. Name is its full
// service name.
type Vehicle struct {
	Name    string   `json:"name"`
	Streams []Stream `json:"streams"`
	Typed   bool     `json:"typed"`
	Problem string   `json:"problem,omitempty"`
}

// Daemon is a daemon and the vehicles and engines it runs.
type Daemon struct {
	IP       string    `json:"ip"`
	Hostname string    `json:"hostname"`
	Local    bool      `json:"local"`
	Vehicles []Vehicle `json:"vehicles"`
	Engines  []Engine  `json:"engines"`
}

// Catalog is what could contribute to a flow, by daemon.
type Catalog struct {
	// Daemons are the GCS's own daemon first, then the others by IP.
	Daemons     []Daemon            `json:"daemons"`
	Unavailable []fleet.Unavailable `json:"unavailable"`
}

// Engine returns the engine with the full service name.
func (c Catalog) Engine(full string) (Engine, bool) {
	for _, d := range c.Daemons {
		for _, e := range d.Engines {
			if e.Name == full {
				return e, true
			}
		}
	}
	return Engine{}, false
}

// Daemon returns the daemon with the IP.
func (c Catalog) Daemon(ip string) (Daemon, bool) {
	for _, d := range c.Daemons {
		if d.IP == ip {
			return d, true
		}
	}
	return Daemon{}, false
}

// VehicleByName returns the vehicle with the full service name.
func (c Catalog) VehicleByName(name string) (Vehicle, bool) {
	for _, d := range c.Daemons {
		for _, v := range d.Vehicles {
			if v.Name == name {
				return v, true
			}
		}
	}
	return Vehicle{}, false
}

// BuildCatalog groups the services of resp by the daemon they run on. gcsIP
// is the IP of the GCS's own daemon and gcsHostname its hostname (the daemon's
// configured one). A service whose plugin config can't be read is listed
// untyped, with the reason in its problem.
func BuildCatalog(resp *daemonpb.ListServicesResponse, gcsIP, gcsHostname string) Catalog {
	byIP := map[string]*Daemon{}
	get := func(ip string) *Daemon {
		d, ok := byIP[ip]
		if !ok {
			d = &Daemon{IP: ip, Local: ip == gcsIP, Vehicles: []Vehicle{}, Engines: []Engine{}}
			byIP[ip] = d
		}
		return d
	}
	cat := Catalog{Daemons: []Daemon{}, Unavailable: []fleet.Unavailable{}}
	hostnames := map[string]string{}
	for _, r := range resp.GetRemotes() {
		if r.GetError() != "" {
			cat.Unavailable = append(cat.Unavailable, fleet.Unavailable{IP: r.GetIp(), Hostname: r.GetHostname(), Reason: r.GetError()})
			continue
		}
		hostnames[r.GetIp()] = r.GetHostname()
		get(r.GetIp())
	}
	for _, s := range resp.GetServices() {
		ip, bare := fleet.SplitName(s.GetName())
		switch s.GetCategory() {
		case daemonpb.ServiceCategory_SERVICE_CATEGORY_VEHICLE:
			d := get(ip)
			d.Vehicles = append(d.Vehicles, buildVehicle(s.GetName(), s.GetConfig()))
		case daemonpb.ServiceCategory_SERVICE_CATEGORY_ENGINE:
			d := get(ip)
			d.Engines = append(d.Engines, buildEngine(s.GetName(), bare, s.GetConfig()))
		}
	}
	for ip, d := range byIP {
		d.Hostname = hostnames[ip]
		if d.Local {
			d.Hostname = gcsHostname
		}
		sort.Slice(d.Vehicles, func(i, j int) bool { return d.Vehicles[i].Name < d.Vehicles[j].Name })
		sort.Slice(d.Engines, func(i, j int) bool { return d.Engines[i].Name < d.Engines[j].Name })
		cat.Daemons = append(cat.Daemons, *d)
	}
	sort.Slice(cat.Daemons, func(i, j int) bool {
		a, b := cat.Daemons[i], cat.Daemons[j]
		if a.Local != b.Local {
			return a.Local
		}
		return a.IP < b.IP
	})
	return cat
}

func buildVehicle(name string, conf []byte) Vehicle {
	streams, problems, err := ParseDriver(conf)
	if err != nil {
		// Every vehicle streams telemetry; its cameras are unknown.
		return Vehicle{Name: name, Streams: []Stream{telemetryStream()}, Problem: err.Error()}
	}
	return Vehicle{Name: name, Streams: streams, Typed: true, Problem: strings.Join(problems, "; ")}
}

func buildEngine(name, bare string, conf []byte) Engine {
	e, err := ParseEngine(conf)
	if err != nil {
		e = untypedEngine(err.Error())
	}
	e.Name, e.Bare = name, bare
	return e
}

// lister is the part of the daemon client the catalog needs.
type lister interface {
	ListServices(ctx context.Context, in *daemonpb.ListServicesRequest, opts ...grpc.CallOption) (*daemonpb.ListServicesResponse, error)
}

// CatalogHandler serves the catalog.
type CatalogHandler struct {
	daemon      lister
	gcsIP       string
	gcsHostname string
	log         zerolog.Logger
}

// NewCatalogHandler returns a handler that lists the catalog from the daemon.
// gcsIP and gcsHostname are the IP and hostname of the GCS's own daemon.
func NewCatalogHandler(c daemonpb.DaemonServiceClient, gcsIP, gcsHostname string) *CatalogHandler {
	return &CatalogHandler{daemon: c, gcsIP: gcsIP, gcsHostname: gcsHostname, log: zerolog.Nop()}
}

// ServeCatalog writes the catalog as JSON. It responds 502 with a detail if
// the daemon can't be listed.
func (h *CatalogHandler) ServeCatalog(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), listTimeout)
	defer cancel()
	resp, err := h.daemon.ListServices(ctx, &daemonpb.ListServicesRequest{})
	if err != nil {
		h.log.Warn().Err(err).Msg("list services for the flow catalog")
		writeJSON(w, http.StatusBadGateway, map[string]string{"detail": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, BuildCatalog(resp, h.gcsIP, h.gcsHostname))
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
