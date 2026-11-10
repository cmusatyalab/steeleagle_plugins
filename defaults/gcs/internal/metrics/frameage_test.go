package metrics

import (
	"math"
	"testing"
	"time"

	framepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/frame"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
	dto "github.com/prometheus/client_model/go"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const vehicle = "100.101.84.40.fake-drone"

func frameAt(capture time.Time) *framepb.EncodedFrame {
	return framepb.EncodedFrame_builder{Timestamp: timestamppb.New(capture)}.Build()
}

func newFrameAge(t *testing.T) (*FrameAge, *prometheus.Registry) {
	t.Helper()
	reg := prometheus.NewRegistry()
	return NewFrameAge(reg), reg
}

func TestFrameAgeObservesArrivalMinusCapture(t *testing.T) {
	ages, reg := newFrameAge(t)
	capture := time.Unix(1_700_000_000, 0)

	ages.Observe(vehicle, frameAt(capture), capture.Add(144*time.Millisecond))

	if got := histogramCount(t, reg); got != 1 {
		t.Fatalf("count = %d, want 1", got)
	}
	if got := histogramSum(t, reg); math.Abs(got-0.144) > 1e-9 {
		t.Fatalf("sum = %v, want 0.144", got)
	}
}

func TestFrameWithoutTimestampIsNotObserved(t *testing.T) {
	ages, reg := newFrameAge(t)

	ages.Observe(vehicle, framepb.EncodedFrame_builder{}.Build(), time.Now())

	if got := histogramCount(t, reg); got != 0 {
		t.Fatalf("count = %d, want 0", got)
	}
}

func TestFutureTimestampCountsAsNegative(t *testing.T) {
	ages, reg := newFrameAge(t)
	at := time.Unix(1_700_000_000, 0)

	ages.Observe(vehicle, frameAt(at.Add(10*time.Millisecond)), at)

	if got := histogramCount(t, reg); got != 0 {
		t.Fatalf("histogram count = %d, want 0", got)
	}
	if got := testutil.ToFloat64(ages.negative.WithLabelValues(vehicle)); got != 1 {
		t.Fatalf("negative = %v, want 1", got)
	}
}

func histogramCount(t *testing.T, reg *prometheus.Registry) uint64 {
	t.Helper()
	h := histogram(t, reg)
	if h == nil {
		return 0
	}
	return h.GetSampleCount()
}

// histogram returns the vehicle's frame age histogram from reg, or nil if
// nothing was observed for it.
func histogram(t *testing.T, reg *prometheus.Registry) *dto.Histogram {
	t.Helper()
	families, err := reg.Gather()
	if err != nil {
		t.Fatal(err)
	}
	for _, f := range families {
		if f.GetName() != "gcs_frame_age_seconds" {
			continue
		}
		for _, m := range f.GetMetric() {
			for _, l := range m.GetLabel() {
				if l.GetName() == "vehicle" && l.GetValue() == vehicle {
					return m.GetHistogram()
				}
			}
		}
	}
	return nil
}

func histogramSum(t *testing.T, reg *prometheus.Registry) float64 {
	t.Helper()
	h := histogram(t, reg)
	if h == nil {
		t.Fatal("no histogram")
	}
	return h.GetSampleSum()
}
