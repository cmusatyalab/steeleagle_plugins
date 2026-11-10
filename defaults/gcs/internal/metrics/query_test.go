package metrics

import (
	"context"
	"errors"
	"math"
	"strings"
	"testing"
	"time"

	v1 "github.com/prometheus/client_golang/api/prometheus/v1"
	"github.com/prometheus/common/model"
)

// answer is what fakeProm returns for queries containing match.
type answer struct {
	match  string
	vector model.Vector
}

// fakeProm answers each query with the vector of the first answer whose
// match it contains, or an empty vector, and records the queries.
type fakeProm struct {
	answers []answer
	err     error
	queries []string
}

func (f *fakeProm) Query(_ context.Context, query string, _ time.Time, _ ...v1.Option) (model.Value, v1.Warnings, v1.Infos, error) {
	f.queries = append(f.queries, query)
	if f.err != nil {
		return nil, nil, nil, f.err
	}
	for _, a := range f.answers {
		if strings.Contains(query, a.match) {
			return a.vector, nil, nil, nil
		}
	}
	return model.Vector{}, nil, nil, nil
}

func sample(engine string, value float64) *model.Sample {
	return &model.Sample{Metric: model.Metric{engineIDLabel: model.LabelValue(engine)}, Value: model.SampleValue(value)}
}

func scalar(value float64) model.Vector {
	return model.Vector{&model.Sample{Metric: model.Metric{}, Value: model.SampleValue(value)}}
}

// Answers distinguish the queries by a part only that query has; skipped
// comes before inputs/s, whose metric it also uses.
func encoderAnswers() []answer {
	return []answer{
		{"histogram_quantile(0.5, sum by (le, engine_id)", model.Vector{sample(encoder, 0.082)}},
		{"histogram_quantile(0.95, sum by (le, engine_id)", model.Vector{sample(encoder, math.NaN())}},
		{`outcome="skipped"`, model.Vector{sample(encoder, 0.59)}},
		{"uriel_engine_inputs_received_total", model.Vector{sample(encoder, 12.2)}},
		{"uriel_engine_processing_latency_seconds_sum", model.Vector{sample(encoder, 0.98)}},
		{"histogram_quantile(0.5, sum by (le) (rate(gcs_frame_age", scalar(0.144)},
		{"histogram_quantile(0.95, sum by (le) (rate(gcs_frame_age", scalar(0.181)},
	}
}

func filled(t *testing.T, prom *fakeProm, window string) View {
	t.Helper()
	v, err := BuildView([]byte(gcsFlow), fakeDrone, testHostnames)
	if err != nil {
		t.Fatal(err)
	}
	if err := Fill(context.Background(), prom, &v, fakeDrone, window); err != nil {
		t.Fatal(err)
	}
	return v
}

func TestFillUsesTheWindowInEveryQuery(t *testing.T) {
	prom := &fakeProm{}

	filled(t, prom, "5m")

	if len(prom.queries) == 0 {
		t.Fatal("no queries")
	}
	for _, q := range prom.queries {
		if !strings.Contains(q, "[5m]") {
			t.Errorf("query %q has no [5m] window", q)
		}
	}
}

func TestFillReadsEachEnginesNumbers(t *testing.T) {
	v := filled(t, &fakeProm{answers: encoderAnswers()}, "1m")

	m := nodeByID(t, v, encoder).Metrics
	if m == nil {
		t.Fatal("encoder has no metrics")
	}
	for name, got := range map[string]*float64{"p50": m.P50, "inputs/s": m.InputsPerSecond, "skipped": m.Skipped, "busy": m.Busy} {
		if got == nil {
			t.Errorf("%s is null", name)
		}
	}
	if *m.P50 != 0.082 || *m.InputsPerSecond != 12.2 || *m.Skipped != 0.59 || *m.Busy != 0.98 {
		t.Errorf("encoder metrics = p50 %v, inputs/s %v, skipped %v, busy %v", *m.P50, *m.InputsPerSecond, *m.Skipped, *m.Busy)
	}
}

func TestFillLeavesNaNAndMissingSamplesNull(t *testing.T) {
	v := filled(t, &fakeProm{answers: encoderAnswers()}, "1m")

	m := nodeByID(t, v, encoder).Metrics
	if m.P95 != nil {
		t.Errorf("p95 = %v, want null for NaN", *m.P95)
	}
	if m.Wait != nil {
		t.Errorf("wait = %v, want null with no sample", *m.Wait)
	}
	if got := nodeByID(t, v, gcs).Metrics; got != nil {
		t.Errorf("gcs metrics = %+v, want null with no samples", got)
	}
	if got := nodeByID(t, v, "rgb").Metrics; got != nil {
		t.Errorf("producer metrics = %+v, want null", got)
	}
}

func TestFillReadsEndToEndForTheVehicle(t *testing.T) {
	prom := &fakeProm{answers: encoderAnswers()}

	v := filled(t, prom, "1m")

	if v.EndToEnd == nil || v.EndToEnd.P50 == nil || *v.EndToEnd.P50 != 0.144 || *v.EndToEnd.P95 != 0.181 {
		t.Fatalf("end to end = %+v, want p50 0.144, p95 0.181", v.EndToEnd)
	}
	var vehicleQueries int
	for _, q := range prom.queries {
		if strings.Contains(q, "histogram_quantile") && strings.Contains(q, `vehicle="`+fakeDrone+`"`) {
			vehicleQueries++
		}
	}
	if vehicleQueries != 2 {
		t.Errorf("%d end-to-end quantile queries select the vehicle, want 2 (p50 and p95)", vehicleQueries)
	}
}

func TestFillReportsAnUnreachablePrometheus(t *testing.T) {
	v, _ := BuildView([]byte(gcsFlow), fakeDrone, testHostnames)

	err := Fill(context.Background(), &fakeProm{err: errors.New("connection refused")}, &v, fakeDrone, "1m")

	if err == nil {
		t.Fatal("want an error")
	}
	for _, n := range v.Nodes {
		if n.Metrics != nil {
			t.Errorf("node %s has metrics %+v, want none", n.ID, n.Metrics)
		}
	}
}

func rttAnswers() []answer {
	return append([]answer{
		{"min_over_time(uriel_remote_rtt_seconds", scalar(0.071)},
		{"avg_over_time(uriel_remote_rtt_seconds", scalar(0.084)},
	}, encoderAnswers()...)
}

func TestFillReadsTheRoundTripTimeOfTheVehiclesDaemon(t *testing.T) {
	prom := &fakeProm{answers: rttAnswers()}

	v := filled(t, prom, "1m")

	if v.Network == nil || v.Network.RTTMin == nil || v.Network.RTTMean == nil {
		t.Fatalf("network = %+v, want min and mean round trip times", v.Network)
	}
	if *v.Network.RTTMin != 0.071 || *v.Network.RTTMean != 0.084 {
		t.Errorf("round trip = min %v, mean %v; want 0.071, 0.084", *v.Network.RTTMin, *v.Network.RTTMean)
	}
	var daemonQueries int
	for _, q := range prom.queries {
		if strings.Contains(q, "uriel_remote_rtt_seconds") && strings.Contains(q, `daemon_ip="`+fermiIP+`"`) {
			daemonQueries++
		}
	}
	if daemonQueries != 2 {
		t.Errorf("%d round trip queries select the vehicle's daemon, want 2 (min and mean)", daemonQueries)
	}
}

func TestNoNetworkWithoutRoundTripSamples(t *testing.T) {
	v := filled(t, &fakeProm{answers: encoderAnswers()}, "1m")

	if v.Network != nil {
		t.Errorf("network = %+v, want null without samples", v.Network)
	}
}

func TestNoNetworkWhenNoEdgeCrossesIt(t *testing.T) {
	const localFlow = `
[[producers]]
name = "rgb"

[[engines]]
name = "100.101.84.40.encoder"
location = "local"

[[edges]]
from = "rgb"
to = "100.101.84.40.encoder"
timeout = "1s"
`
	v, err := BuildView([]byte(localFlow), fakeDrone, testHostnames)
	if err != nil {
		t.Fatal(err)
	}

	if err := Fill(context.Background(), &fakeProm{answers: rttAnswers()}, &v, fakeDrone, "1m"); err != nil {
		t.Fatal(err)
	}

	if v.Network != nil {
		t.Errorf("network = %+v, want null for a flow that stays on the vehicle", v.Network)
	}
}

func TestFillReportsTheShareOfFramesStampedInTheFuture(t *testing.T) {
	answers := append([]answer{
		{"gcs_frame_age_negative_total", scalar(5)},
		{"gcs_frame_age_seconds_count", scalar(95)},
	}, encoderAnswers()...)

	v := filled(t, &fakeProm{answers: answers}, "1m")

	if v.ClockSkewShare == nil || math.Abs(*v.ClockSkewShare-0.05) > 1e-9 {
		t.Fatalf("clock skew share = %v, want 0.05", v.ClockSkewShare)
	}
}

func TestNoClockSkewWhenNoFrameIsStampedInTheFuture(t *testing.T) {
	answers := append([]answer{
		{"gcs_frame_age_negative_total", scalar(0)},
		{"gcs_frame_age_seconds_count", scalar(95)},
	}, encoderAnswers()...)

	v := filled(t, &fakeProm{answers: answers}, "1m")

	if v.ClockSkewShare != nil {
		t.Errorf("clock skew share = %v, want null", *v.ClockSkewShare)
	}
}
