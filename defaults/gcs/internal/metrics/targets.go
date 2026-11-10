package metrics

import (
	"context"
	"net"
	"net/http"
	"sort"
	"strconv"
	"sync"

	"github.com/BurntSushi/toml"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/daemons"
	"github.com/rs/zerolog"
)

// Labels Prometheus attaches to each daemon's samples.
const (
	daemonIPLabel = "daemon_ip"
	hostnameLabel = "hostname"
)

// DaemonConfigs lists the daemons the GCS's daemon knows of, and reads each
// one's config.
type DaemonConfigs interface {
	List(ctx context.Context) ([]daemons.Daemon, error)
	ReadConfig(ctx context.Context, ip string) ([]byte, error)
}

// TargetGroup is one entry of Prometheus's HTTP service discovery: the
// addresses to scrape and the labels to put on their samples.
type TargetGroup struct {
	Targets []string          `json:"targets"`
	Labels  map[string]string `json:"labels"`
}

// MetricsPort is the port a daemon serves its Prometheus metrics on, from
// [daemon] metrics_port in its config TOML; 0 if it serves none.
func MetricsPort(configTOML []byte) (int, error) {
	var config struct {
		Daemon struct {
			MetricsPort int `toml:"metrics_port"`
		} `toml:"daemon"`
	}
	if _, err := toml.Decode(string(configTOML), &config); err != nil {
		return 0, err
	}
	return config.Daemon.MetricsPort, nil
}

// Targets lists, in IP order, a target group for each daemon that serves
// metrics. A daemon that reports an error, whose config can't be read, or
// that has no metrics port is left out, so it costs Prometheus nothing.
func Targets(ctx context.Context, d DaemonConfigs) []TargetGroup {
	listed, err := d.List(ctx)
	if err != nil {
		return nil
	}
	groups := make([]*TargetGroup, len(listed))
	var wg sync.WaitGroup
	for i, daemon := range listed {
		if daemon.Error != "" {
			continue
		}
		wg.Go(func() {
			raw, err := d.ReadConfig(ctx, daemon.IP)
			if err != nil {
				return
			}
			port, err := MetricsPort(raw)
			if err != nil || port == 0 {
				return
			}
			groups[i] = &TargetGroup{
				Targets: []string{net.JoinHostPort(daemon.IP, strconv.Itoa(port))},
				Labels:  map[string]string{daemonIPLabel: daemon.IP, hostnameLabel: daemon.Hostname},
			}
		})
	}
	wg.Wait()
	out := []TargetGroup{}
	for _, g := range groups {
		if g != nil {
			out = append(out, *g)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Labels[daemonIPLabel] < out[j].Labels[daemonIPLabel] })
	return out
}

// NewTargetsHandler serves GET /api/metrics/targets, Prometheus's HTTP
// service discovery of the daemons to scrape. It always answers a list,
// empty if the daemons can't be listed, since Prometheus drops every target
// on an error.
func NewTargetsHandler(d DaemonConfigs, log zerolog.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		groups := Targets(r.Context(), d)
		if groups == nil {
			log.Warn().Msg("could not list the daemons to scrape")
			groups = []TargetGroup{}
		}
		writeJSON(w, http.StatusOK, groups)
	}
}
