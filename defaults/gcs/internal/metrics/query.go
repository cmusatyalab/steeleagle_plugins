package metrics

import (
	"context"
	"fmt"
	"math"
	"slices"
	"strconv"
	"time"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	v1 "github.com/prometheus/client_golang/api/prometheus/v1"
	"github.com/prometheus/common/model"
)

// Windows are the time windows the numbers can cover, in PromQL's duration
// syntax.
var Windows = []string{"1m", "5m", "15m", "1h"}

// DefaultWindow is the window used when none is asked for.
const DefaultWindow = "1m"

// The quantiles the times are shown at.
const (
	QuantileMedian = 0.5
	QuantileTail   = 0.95
)

// engineIDLabel is the label Uriel puts on each engine's series.
const engineIDLabel = "engine_id"

// Querier runs an instant PromQL query, as the Prometheus API client does.
type Querier interface {
	Query(ctx context.Context, query string, ts time.Time, opts ...v1.Option) (model.Value, v1.Warnings, v1.Infos, error)
}

// engineQuery is one per-engine number: its PromQL, given the window, and
// where it goes.
type engineQuery struct {
	promql func(window string) string
	field  func(m *EngineMetrics) **float64
}

func quantileQuery(q float64) func(string) string {
	return func(w string) string {
		return fmt.Sprintf("histogram_quantile(%s, sum by (le, engine_id) (rate(uriel_engine_processing_latency_seconds_bucket[%s])))", formatQuantile(q), w)
	}
}

var engineQueries = []engineQuery{
	{quantileQuery(QuantileMedian), func(m *EngineMetrics) **float64 { return &m.P50 }},
	{quantileQuery(QuantileTail), func(m *EngineMetrics) **float64 { return &m.P95 }},
	{func(w string) string {
		return fmt.Sprintf("sum by (engine_id) (rate(uriel_engine_wait_seconds_sum[%s])) / sum by (engine_id) (rate(uriel_engine_wait_seconds_count[%s]))", w, w)
	}, func(m *EngineMetrics) **float64 { return &m.Wait }},
	{func(w string) string {
		return fmt.Sprintf("sum by (engine_id) (rate(uriel_engine_inputs_received_total[%s]))", w)
	}, func(m *EngineMetrics) **float64 { return &m.InputsPerSecond }},
	{func(w string) string {
		skipped := fmt.Sprintf(`sum by (engine_id) (increase(uriel_engine_outcomes_total{outcome="skipped"}[%s]))`, w)
		received := fmt.Sprintf("sum by (engine_id) (increase(uriel_engine_inputs_received_total[%s]))", w)
		return fmt.Sprintf("%s / (%s + %s)", skipped, skipped, received)
	}, func(m *EngineMetrics) **float64 { return &m.Skipped }},
	{func(w string) string {
		return fmt.Sprintf("sum by (engine_id) (rate(uriel_engine_processing_latency_seconds_sum[%s]))", w)
	}, func(m *EngineMetrics) **float64 { return &m.Busy }},
}

func endToEndQuery(q float64, vehicle, window string) string {
	return fmt.Sprintf("histogram_quantile(%s, sum by (le) (rate(gcs_frame_age_seconds_bucket{vehicle=%s}[%s])))",
		formatQuantile(q), strconv.Quote(vehicle), window)
}

// frameCountQueries count the vehicle's frames over the window: those
// stamped in the future, and those observed into the age histogram.
func frameCountQueries(vehicle, window string) (future, observed string) {
	selector := fmt.Sprintf("{vehicle=%s}[%s]", strconv.Quote(vehicle), window)
	return "sum(increase(gcs_frame_age_negative_total" + selector + "))",
		"sum(increase(gcs_frame_age_seconds_count" + selector + "))"
}

// rttQueries are the minimum and the mean over the window of the round trip
// time between the vehicle's daemon and its remote server. A daemon has one
// remote server at a time, but its address can change within the window.
func rttQueries(vehicle, window string) (minimum, mean string) {
	ip, _ := fleet.SplitName(vehicle)
	selector := fmt.Sprintf("uriel_remote_rtt_seconds{%s=%s}[%s]", daemonIPLabel, strconv.Quote(ip), window)
	return "min(min_over_time(" + selector + "))", "avg(avg_over_time(" + selector + "))"
}

func formatQuantile(q float64) string {
	return strconv.FormatFloat(q, 'f', -1, 64)
}

// Fill sets the numbers of v's engines, and v's end-to-end time, over the
// window ending now. An error means Prometheus could not be asked; v is then
// left without numbers.
func Fill(ctx context.Context, q Querier, v *View, vehicle, window string) error {
	now := time.Now()
	byEngine := map[string]*EngineMetrics{}
	for _, eq := range engineQueries {
		samples, err := vector(ctx, q, eq.promql(window), now)
		if err != nil {
			return err
		}
		for _, s := range samples {
			engine := string(s.Metric[engineIDLabel])
			value := finite(s.Value)
			if value == nil {
				continue
			}
			m := byEngine[engine]
			if m == nil {
				m = &EngineMetrics{}
				byEngine[engine] = m
			}
			*eq.field(m) = value
		}
	}
	var e2e Latency
	for _, part := range []struct {
		quantile float64
		field    **float64
	}{{QuantileMedian, &e2e.P50}, {QuantileTail, &e2e.P95}} {
		samples, err := vector(ctx, q, endToEndQuery(part.quantile, vehicle, window), now)
		if err != nil {
			return err
		}
		if len(samples) > 0 {
			*part.field = finite(samples[0].Value)
		}
	}

	skew, err := clockSkewShare(ctx, q, vehicle, window, now)
	if err != nil {
		return err
	}

	var network *Network
	if slices.ContainsFunc(v.Edges, func(e Edge) bool { return e.CrossesNetwork }) {
		if network, err = roundTrip(ctx, q, vehicle, window, now); err != nil {
			return err
		}
	}

	for i := range v.Nodes {
		if v.Nodes[i].Kind == KindEngine {
			v.Nodes[i].Metrics = byEngine[v.Nodes[i].ID]
		}
	}
	if e2e.P50 != nil || e2e.P95 != nil {
		v.EndToEnd = &e2e
	}
	v.ClockSkewShare = skew
	v.Network = network
	return nil
}

// roundTrip is the network between the vehicle's daemon and its remote server
// over the window, or nil if there are no samples.
func roundTrip(ctx context.Context, q Querier, vehicle, window string, at time.Time) (*Network, error) {
	var n Network
	minimum, mean := rttQueries(vehicle, window)
	for _, part := range []struct {
		promql string
		field  **float64
	}{{minimum, &n.RTTMin}, {mean, &n.RTTMean}} {
		samples, err := vector(ctx, q, part.promql, at)
		if err != nil {
			return nil, err
		}
		if len(samples) > 0 {
			*part.field = finite(samples[0].Value)
		}
	}
	if n.RTTMin == nil && n.RTTMean == nil {
		return nil, nil
	}
	return &n, nil
}

// clockSkewShare is the share of the vehicle's frames in the window that
// were stamped in the future, or nil if none were.
func clockSkewShare(ctx context.Context, q Querier, vehicle, window string, at time.Time) (*float64, error) {
	futureQuery, observedQuery := frameCountQueries(vehicle, window)
	count := func(promql string) (float64, error) {
		samples, err := vector(ctx, q, promql, at)
		if err != nil || len(samples) == 0 {
			return 0, err
		}
		if f := finite(samples[0].Value); f != nil {
			return *f, nil
		}
		return 0, nil
	}
	future, err := count(futureQuery)
	if err != nil {
		return nil, err
	}
	observed, err := count(observedQuery)
	if err != nil {
		return nil, err
	}
	if future <= 0 {
		return nil, nil
	}
	share := future / (future + observed)
	return &share, nil
}

func vector(ctx context.Context, q Querier, promql string, at time.Time) (model.Vector, error) {
	value, _, _, err := q.Query(ctx, promql, at)
	if err != nil {
		return nil, err
	}
	samples, ok := value.(model.Vector)
	if !ok {
		return nil, fmt.Errorf("query %q answered a %s, not a vector", promql, value.Type())
	}
	return samples, nil
}

// finite is value, or nil if it is NaN or infinite, which is how PromQL says
// there was nothing to compute from (e.g. a quantile of no observations).
func finite(value model.SampleValue) *float64 {
	f := float64(value)
	if math.IsNaN(f) || math.IsInf(f, 0) {
		return nil
	}
	return &f
}
