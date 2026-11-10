package metrics

import (
	"context"
	"os"
	"testing"

	promapi "github.com/prometheus/client_golang/api"
	promv1 "github.com/prometheus/client_golang/api/prometheus/v1"
)

// prometheusURLEnv names a running Prometheus server to check the queries
// against; the test is skipped without one.
const prometheusURLEnv = "PROMETHEUS_URL"

// TestQueriesAreValidPromQL runs every query Fill makes against a real
// Prometheus, which rejects any that doesn't parse. With no data the
// numbers are all null, so it checks only that Prometheus answered.
func TestQueriesAreValidPromQL(t *testing.T) {
	url := os.Getenv(prometheusURLEnv)
	if url == "" {
		t.Skipf("set %s to a Prometheus server to run", prometheusURLEnv)
	}
	client, err := promapi.NewClient(promapi.Config{Address: url})
	if err != nil {
		t.Fatal(err)
	}
	for _, window := range Windows {
		v, _ := BuildView([]byte(gcsFlow), fakeDrone, testHostnames)
		if err := Fill(context.Background(), promv1.NewAPI(client), &v, fakeDrone, window); err != nil {
			t.Errorf("window %s: %v", window, err)
		}
	}
}
