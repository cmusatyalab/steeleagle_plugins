package metrics

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/daemons"
	v1 "github.com/prometheus/client_golang/api/prometheus/v1"
	"github.com/prometheus/common/model"
	"github.com/rs/zerolog"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func flowOf(toml string, err error) func(context.Context, string) ([]byte, error) {
	return func(context.Context, string) ([]byte, error) { return []byte(toml), err }
}

var twoDaemons = fakeDaemons{daemons: []daemons.Daemon{{IP: fermiIP, Hostname: "fermi"}, {IP: lambda2IP, Hostname: "lambda2"}}}

func get(t *testing.T, h http.HandlerFunc, query string) (*httptest.ResponseRecorder, View) {
	t.Helper()
	rec := httptest.NewRecorder()
	h(rec, httptest.NewRequest(http.MethodGet, "/api/metrics/flow?"+query, nil))
	var v View
	if rec.Code == http.StatusOK {
		if err := json.Unmarshal(rec.Body.Bytes(), &v); err != nil {
			t.Fatal(err)
		}
	}
	return rec, v
}

func TestRejectsAnUnknownWindowOrNoVehicle(t *testing.T) {
	h := NewFlowHandler(flowOf(gcsFlow, nil), twoDaemons, &fakeProm{}, zerolog.Nop())

	if rec, _ := get(t, h, "vehicle="+fakeDrone+"&window=2m"); rec.Code != http.StatusBadRequest {
		t.Errorf("window=2m: %d, want 400", rec.Code)
	}
	if rec, _ := get(t, h, "window=1m"); rec.Code != http.StatusBadRequest {
		t.Errorf("no vehicle: %d, want 400", rec.Code)
	}
}

func TestWindowDefaultsToOneMinute(t *testing.T) {
	h := NewFlowHandler(flowOf(gcsFlow, nil), twoDaemons, &fakeProm{}, zerolog.Nop())

	rec, v := get(t, h, "vehicle="+fakeDrone)

	if rec.Code != http.StatusOK || v.Window != DefaultWindow {
		t.Errorf("got %d, window %q; want 200, %q", rec.Code, v.Window, DefaultWindow)
	}
}

func TestUnknownVehicleIsNotFound(t *testing.T) {
	h := NewFlowHandler(flowOf("", status.Error(codes.NotFound, "no such vehicle")), twoDaemons, &fakeProm{}, zerolog.Nop())

	if rec, _ := get(t, h, "vehicle=ghost"); rec.Code != http.StatusNotFound {
		t.Errorf("got %d, want 404", rec.Code)
	}
}

func TestNoFlowState(t *testing.T) {
	h := NewFlowHandler(flowOf("", nil), twoDaemons, &fakeProm{}, zerolog.Nop())

	rec, v := get(t, h, "vehicle="+fakeDrone)

	if rec.Code != http.StatusOK || v.State != StateNoFlow || len(v.Nodes) != 0 {
		t.Errorf("got %d %+v, want 200 with state %q", rec.Code, v, StateNoFlow)
	}
}

func TestOKStateMarksTheBottleneck(t *testing.T) {
	h := NewFlowHandler(flowOf(gcsFlow, nil), twoDaemons, &fakeProm{answers: encoderAnswers()}, zerolog.Nop())

	rec, v := get(t, h, "vehicle="+fakeDrone+"&window=5m")

	if rec.Code != http.StatusOK || v.State != StateOK || v.Window != "5m" || v.Vehicle != fakeDrone {
		t.Fatalf("got %d %+v", rec.Code, v)
	}
	if !nodeByID(t, v, encoder).Bottleneck || nodeByID(t, v, encoder).Host != "fermi" {
		t.Errorf("encoder = %+v, want the bottleneck on fermi", nodeByID(t, v, encoder))
	}
}

func TestPrometheusDownStillReturnsTheFlow(t *testing.T) {
	h := NewFlowHandler(flowOf(gcsFlow, nil), twoDaemons, &fakeProm{err: errors.New("connection refused")}, zerolog.Nop())

	rec, v := get(t, h, "vehicle="+fakeDrone)

	if rec.Code != http.StatusOK || v.State != StatePrometheusUnreachable || len(v.Nodes) != 4 || v.Detail == "" {
		t.Fatalf("got %d %+v, want 200, state %q, 4 nodes and a detail", rec.Code, v, StatePrometheusUnreachable)
	}
	for _, n := range v.Nodes {
		if n.Metrics != nil {
			t.Errorf("node %s has metrics, want none", n.ID)
		}
	}
}

func TestFlowThatCantBeReadIsABadGateway(t *testing.T) {
	h := NewFlowHandler(flowOf("", status.Error(codes.Unavailable, "daemon down")), twoDaemons, &fakeProm{}, zerolog.Nop())

	if rec, _ := get(t, h, "vehicle="+fakeDrone); rec.Code != http.StatusBadGateway {
		t.Errorf("got %d, want 502", rec.Code)
	}
}

// hungProm answers no query until its context ends, as a stopped or
// overloaded Prometheus does.
type hungProm struct{ fakeProm }

func (h *hungProm) Query(ctx context.Context, query string, ts time.Time, opts ...v1.Option) (model.Value, v1.Warnings, v1.Infos, error) {
	<-ctx.Done()
	return nil, nil, nil, ctx.Err()
}

func TestHungPrometheusTimesOutAsUnreachable(t *testing.T) {
	h := NewFlowHandler(flowOf(gcsFlow, nil), twoDaemons, &hungProm{}, zerolog.Nop())
	started := time.Now()

	rec, v := get(t, h, "vehicle="+fakeDrone)

	if took := time.Since(started); took > QueryTimeout+time.Second {
		t.Errorf("took %v, want at most about %v", took, QueryTimeout)
	}
	if rec.Code != http.StatusOK || v.State != StatePrometheusUnreachable || len(v.Nodes) != 4 {
		t.Errorf("got %d %+v, want 200, state %q with the flow", rec.Code, v, StatePrometheusUnreachable)
	}
}
