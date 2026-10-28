package fleet

import (
	"encoding/json"
	"math"
	"net/http"
	"sort"
	"sync"
	"time"

	telemetrypb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/telemetry"
	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"github.com/rs/zerolog"
	"google.golang.org/grpc"
)

const (
	// staleSeconds is the last_updated of a vehicle with no telemetry.
	staleSeconds = 1e6
)

// Location is a position in the shape the UI reads.
type Location struct {
	Lat  float64 `json:"lat"`
	Long float64 `json:"long"`
	Alt  float64 `json:"alt"`
}

// Velocity is a body-frame velocity in the shape the UI reads.
type Velocity struct {
	XVel       float64 `json:"x_vel"`
	YVel       float64 `json:"y_vel"`
	ZVel       float64 `json:"z_vel"`
	AngularVel float64 `json:"angular_vel"`
}

// VehicleJSON is a vehicle as the UI reads it.
type VehicleJSON struct {
	Name        string    `json:"name"`
	Model       string    `json:"model"`
	Type        string    `json:"type"`
	Battery     int       `json:"battery"`
	Sats        int       `json:"sats"`
	Mag         int       `json:"mag"`
	LastUpdated float64   `json:"last_updated"`
	Selected    bool      `json:"selected"`
	Home        *Location `json:"home,omitempty"`
	Current     Location  `json:"current"`
	Bearing     float64   `json:"bearing"`
	Velocity    *Velocity `json:"velocity,omitempty"`
	GCSFlow     string    `json:"gcs_flow"`
}

// ToVehicleJSON maps a vehicle and its latest telemetry to the UI's shape.
// flowStatus is the state of the vehicle's GCS dataflow. A nil t, or one with
// no timestamp, gives zeroed fields and a last_updated of 1e6 seconds.
func ToVehicleJSON(v Vehicle, t *telemetrypb.Telemetry, flowStatus string, now time.Time) VehicleJSON {
	out := VehicleJSON{
		Name: v.Name, Model: v.Plugin, Type: "UAV",
		LastUpdated: staleSeconds, GCSFlow: flowStatus,
	}
	if t == nil {
		return out
	}
	if ts := t.GetTimestamp(); ts != nil {
		out.LastUpdated = now.Sub(ts.AsTime()).Seconds()
	}
	out.Battery = int(t.GetBatteryInfo().GetPercentage())
	out.Sats = int(t.GetGpsInfo().GetSatellites())
	out.Mag = int(t.GetAlertInfo().GetMagnetometerWarning())
	pos := t.GetPositionInfo()
	if h := pos.GetHome(); h != nil {
		out.Home = &Location{Lat: h.GetLatitude(), Long: h.GetLongitude(), Alt: float64(h.GetAltitude())}
	}
	g := pos.GetGlobalPosition()
	out.Current = Location{
		Lat: g.GetLatitude(), Long: g.GetLongitude(),
		Alt: math.Max(0, float64(pos.GetRelativePosition().GetZ())),
	}
	out.Bearing = math.Mod(math.Mod(float64(g.GetHeading()), 360)+360, 360)
	if vel := pos.GetVelocityBody(); vel != nil {
		out.Velocity = &Velocity{
			XVel: float64(vel.GetXVel()), YVel: float64(vel.GetYVel()),
			ZVel: float64(vel.GetZVel()), AngularVel: float64(vel.GetAngularVel()),
		}
	}
	return out
}

// Vehicles serves the vehicle list.
type Vehicles struct {
	daemon daemonpb.DaemonServiceClient
	flows  *Flows
	log    zerolog.Logger

	mu sync.Mutex
	// known are the vehicles last listed, by name: those of a daemon that
	// becomes unavailable are still listed, as disconnected.
	known map[string]Vehicle
	// unavailable are the unavailable daemons last listed, by IP, to log
	// each change once.
	unavailable map[string]Unavailable
}

// NewVehicles returns a handler that lists vehicles from the daemon at conn.
// flows may be nil, in which case no dataflows are managed, gcs_flow is
// empty, and no vehicle has telemetry.
func NewVehicles(conn grpc.ClientConnInterface, flows *Flows, log zerolog.Logger) *Vehicles {
	return &Vehicles{
		daemon:      daemonpb.NewDaemonServiceClient(conn),
		flows:       flows,
		log:         log,
		known:       map[string]Vehicle{},
		unavailable: map[string]Unavailable{},
	}
}

// ServeVehicles writes the running vehicles, sorted by name, as a JSON array.
// Each vehicle's telemetry is the last the GCS received over its flow; the
// daemon is not asked for it. A vehicle whose telemetry the GCS never
// received is listed as stale, as is a vehicle last listed on a
// remote daemon that is now unavailable, which is still listed, with the
// reason in gcs_flow, until its daemon is no longer listed as unavailable. It
// responds 502 if the daemon can't be listed.
func (h *Vehicles) ServeVehicles(w http.ResponseWriter, r *http.Request) {
	snap, err := List(r.Context(), h.daemon)
	if err != nil {
		h.log.Warn().Err(err).Msg("list vehicles")
		writeJSON(w, http.StatusBadGateway, map[string]string{"detail": err.Error()})
		return
	}
	if h.flows != nil {
		h.flows.Observe(snap)
	}
	tels := make([]*telemetrypb.Telemetry, len(snap.Vehicles))
	if h.flows != nil {
		for i, v := range snap.Vehicles {
			tels[i] = h.flows.Telemetry(v.Name)
		}
	}
	now := time.Now()
	out := make([]VehicleJSON, len(snap.Vehicles))
	for i, v := range snap.Vehicles {
		var status string
		if h.flows != nil {
			status = h.flows.Status(v.Name)
		}
		out[i] = ToVehicleJSON(v, tels[i], status, now)
	}
	for _, s := range h.remember(snap) {
		out = append(out, ToVehicleJSON(s.vehicle, nil,
			"error: daemon "+s.daemon.IP+" unavailable: "+s.daemon.Reason, now))
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	writeJSON(w, http.StatusOK, out)
}

// staleVehicle is a vehicle last listed on a daemon now unavailable.
type staleVehicle struct {
	vehicle Vehicle
	daemon  Unavailable
}

// remember records the vehicles and unavailable daemons of snap, logs each
// change in the unavailable daemons, and returns the vehicles last listed
// that aren't in snap but whose daemon is unavailable. It forgets the other
// vehicles that aren't in snap.
func (h *Vehicles) remember(snap Snapshot) []staleVehicle {
	h.mu.Lock()
	defer h.mu.Unlock()
	unavailable := map[string]Unavailable{}
	for _, u := range snap.Unavailable {
		unavailable[u.IP] = u
		if h.unavailable[u.IP] != u {
			h.log.Warn().Str("ip", u.IP).Str("hostname", u.Hostname).Str("reason", u.Reason).
				Msg("remote daemon unavailable")
		}
	}
	for ip, u := range h.unavailable {
		if _, ok := unavailable[ip]; !ok {
			h.log.Info().Str("ip", ip).Str("hostname", u.Hostname).Msg("remote daemon no longer unavailable")
		}
	}
	h.unavailable = unavailable

	listed := map[string]bool{}
	for _, v := range snap.Vehicles {
		listed[v.Name] = true
		h.known[v.Name] = v
	}
	var stale []staleVehicle
	for name, v := range h.known {
		if listed[name] {
			continue
		}
		if u, ok := unavailable[v.DaemonIP]; ok && v.DaemonIP != "" {
			stale = append(stale, staleVehicle{v, u})
		} else {
			delete(h.known, name)
		}
	}
	return stale
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
