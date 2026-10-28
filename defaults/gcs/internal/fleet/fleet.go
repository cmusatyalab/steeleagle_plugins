// Package fleet reads the vehicles and engines the daemon knows about, and
// builds and pushes the dataflows they run.
package fleet

import (
	"context"
	"errors"
	"net/netip"
	"runtime"
	"strings"

	"github.com/cmusatyalab/steeleagle/config"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
)

// errNoDriver is why a vehicle with no driver cap has no driver.
var errNoDriver = errors.New("plugin config has no driver cap")

// SplitName splits a service name into the IP of the daemon it runs on and
// its own name. Services on remote daemons are named with that daemon's IP
// and a dot before their own name; the IP is the longest such prefix. It
// returns an empty IP and the whole name if there is no such prefix.
func SplitName(name string) (daemonIP, bare string) {
	for i := len(name) - 1; i > 0; i-- {
		if name[i] != '.' {
			continue
		}
		if _, err := netip.ParseAddr(name[:i]); err == nil {
			return name[:i], name[i+1:]
		}
	}
	return "", name
}

// Vehicle is a running vehicle service.
type Vehicle struct {
	// Name is the full service name, which is also the vehicle's route key.
	Name string
	// Plugin is the name of the plugin the vehicle runs.
	Plugin string
	// DaemonIP is the IP of the daemon the vehicle runs on, from the prefix
	// of its name, which the daemon always adds (even for its own services).
	// It is empty only if the name has no such prefix.
	DaemonIP string
	// Driver is the vehicle's driver capabilities, or nil if its config
	// could not be read, in which case DriverErr says why.
	Driver    *config.DriverCap
	DriverErr error
	// Config is the vehicle's raw plugin config, as the daemon reported it.
	Config []byte
	// Arch is the GOARCH of the daemon the vehicle runs on. A daemon that
	// the GCS's own daemon lists as a remote reports its own, which is ""
	// if it does not (an older eagled). A daemon that is not listed is the
	// GCS's own daemon, which never lists itself and runs on the GCS's
	// host, so Arch is the GCS's runtime.GOARCH.
	Arch string
}

// Unavailable is a remote daemon the daemon could not reach, whose services
// are therefore not listed.
type Unavailable struct {
	IP       string `json:"ip"`
	Hostname string `json:"hostname"`
	Reason   string `json:"reason"`
}

// Snapshot is the vehicles and engines the daemon listed.
type Snapshot struct {
	// Vehicles are the running vehicle services.
	Vehicles []Vehicle
	// Engines is the set of full names of the engine services.
	Engines map[string]bool
	// Encoders maps the IP of each daemon that runs an H.264 encoder to the
	// full name of its encoder service.
	Encoders map[string]string
	// Unavailable are the remote daemons that could not be reached.
	Unavailable []Unavailable
}

// List lists the daemon's running vehicles and all of its engines, and the
// remote daemons it could not reach. A vehicle whose config can't be read is
// still listed, with DriverErr set.
func List(ctx context.Context, c daemonpb.DaemonServiceClient) (Snapshot, error) {
	resp, err := c.ListServices(ctx, &daemonpb.ListServicesRequest{})
	if err != nil {
		return Snapshot{}, err
	}
	snap := Snapshot{Engines: map[string]bool{}, Encoders: map[string]string{}}
	remoteArch := map[string]string{}
	for _, r := range resp.GetRemotes() {
		remoteArch[r.GetIp()] = r.GetArch()
	}
	for _, s := range resp.GetServices() {
		switch s.GetCategory() {
		case daemonpb.ServiceCategory_SERVICE_CATEGORY_ENGINE:
			snap.Engines[s.GetName()] = true
			if isEncoder(s) {
				// The first by name, so the choice is stable
				ip, _ := SplitName(s.GetName())
				if cur, ok := snap.Encoders[ip]; !ok || s.GetName() < cur {
					snap.Encoders[ip] = s.GetName()
				}
			}
		case daemonpb.ServiceCategory_SERVICE_CATEGORY_VEHICLE:
			if !s.GetRunning() {
				continue
			}
			ip, _ := SplitName(s.GetName())
			v := Vehicle{Name: s.GetName(), Plugin: s.GetPlugin(), DaemonIP: ip, Config: s.GetConfig()}
			if arch, ok := remoteArch[ip]; ok {
				v.Arch = arch
			} else {
				v.Arch = runtime.GOARCH
			}
			if conf, err := config.ParsePluginConfigFromBytes(s.GetConfig()); err != nil {
				v.DriverErr = err
			} else if conf.Driver == nil {
				v.DriverErr = errNoDriver
			} else {
				v.Driver = conf.Driver
			}
			snap.Vehicles = append(snap.Vehicles, v)
		}
	}
	// A remote daemon with an error could not be reached
	for _, r := range resp.GetRemotes() {
		if r.GetError() != "" {
			snap.Unavailable = append(snap.Unavailable, Unavailable{IP: r.GetIp(), Hostname: r.GetHostname(), Reason: r.GetError()})
		}
	}
	return snap, nil
}

// isEncoder reports whether the service runs the H.264 encoder: the
// EncoderPlugin plugin of a package whose directory is EncoderPackage. Its
// service may have any name.
func isEncoder(s *daemonpb.Service) bool {
	if s.GetPlugin() != EncoderPlugin {
		return false
	}
	// A package is a repository, then a double slash and its subdirectory,
	// optionally followed by @version
	pkg := s.GetUri()
	if i := strings.LastIndex(pkg, "@"); i > strings.LastIndex(pkg, "/") {
		pkg = pkg[:i]
	}
	pkg = strings.TrimSuffix(pkg, "/")
	return pkg[strings.LastIndex(pkg, "/")+1:] == EncoderPackage
}
