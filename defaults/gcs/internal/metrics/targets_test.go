package metrics

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/daemons"
	"github.com/rs/zerolog"
)

const (
	fermiIP   = "100.101.84.40"
	lambda2IP = "128.2.208.19"
	withPort  = "[daemon]\nhostname = \"x\"\nmetrics_port = 9393\n"
)

// fakeDaemons lists daemons and answers each one's config from configs; a
// daemon missing from configs fails to answer.
type fakeDaemons struct {
	daemons []daemons.Daemon
	configs map[string]string
	listErr error
}

func (f fakeDaemons) List(context.Context) ([]daemons.Daemon, error) {
	return f.daemons, f.listErr
}

func (f fakeDaemons) ReadConfig(_ context.Context, ip string) ([]byte, error) {
	c, ok := f.configs[ip]
	if !ok {
		return nil, errors.New("daemon unreachable")
	}
	return []byte(c), nil
}

func TestMetricsPortReadsTheDaemonTable(t *testing.T) {
	if port, err := MetricsPort([]byte(withPort)); err != nil || port != 9393 {
		t.Errorf("MetricsPort = %d, %v; want 9393", port, err)
	}
	if port, err := MetricsPort([]byte("[daemon]\nport = 9000\n")); err != nil || port != 0 {
		t.Errorf("MetricsPort without metrics_port = %d, %v; want 0", port, err)
	}
	if _, err := MetricsPort([]byte("[daemon\n")); err == nil {
		t.Error("MetricsPort of invalid TOML: want an error")
	}
}

func TestTargetsListOneGroupPerDaemonWithAPort(t *testing.T) {
	d := fakeDaemons{
		// Listed out of order: targets come in IP order.
		daemons: []daemons.Daemon{{IP: lambda2IP, Hostname: "lambda2"}, {IP: fermiIP, Hostname: "fermi"}},
		configs: map[string]string{fermiIP: withPort, lambda2IP: withPort},
	}

	got := Targets(context.Background(), d)

	want := []TargetGroup{
		{Targets: []string{fermiIP + ":9393"}, Labels: map[string]string{"daemon_ip": fermiIP, "hostname": "fermi"}},
		{Targets: []string{lambda2IP + ":9393"}, Labels: map[string]string{"daemon_ip": lambda2IP, "hostname": "lambda2"}},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("Targets = %+v, want %+v", got, want)
	}
}

func TestDaemonWithoutAPortOrUnreadableConfigIsLeftOut(t *testing.T) {
	d := fakeDaemons{
		daemons: []daemons.Daemon{
			{IP: "10.0.0.1", Hostname: "no-port"},
			{IP: "10.0.0.2", Hostname: "unreachable"},
			{IP: "10.0.0.3", Hostname: "errored", Error: "connection refused"},
			{IP: fermiIP, Hostname: "fermi"},
		},
		configs: map[string]string{"10.0.0.1": "[daemon]\n", "10.0.0.3": withPort, fermiIP: withPort},
	}

	got := Targets(context.Background(), d)

	if len(got) != 1 || got[0].Labels["hostname"] != "fermi" {
		t.Errorf("Targets = %+v, want only fermi", got)
	}
}

func TestTargetsHandlerAnswersAnEmptyListWhenListingFails(t *testing.T) {
	handler := NewTargetsHandler(fakeDaemons{listErr: errors.New("daemon down")}, zerolog.Nop())
	rec := httptest.NewRecorder()

	handler(rec, httptest.NewRequest(http.MethodGet, "/api/metrics/targets", nil))

	if rec.Code != http.StatusOK || strings.TrimSpace(rec.Body.String()) != "[]" {
		t.Errorf("GET targets = %d %q, want 200 []", rec.Code, rec.Body.String())
	}
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", ct)
	}
}
