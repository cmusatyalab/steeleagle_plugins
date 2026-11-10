// Package metrics serves the GCS's Metrics page: the GCS's own metrics, the
// daemons Prometheus scrapes, and each vehicle's running flow with the
// numbers Prometheus has for it.
package metrics

import (
	"time"

	framepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/frame"
	"github.com/prometheus/client_golang/prometheus"
)

// The frame age histogram's buckets: 5 ms doubling 12 times, to about 10 s.
const (
	FrameAgeFirstBucket  = 0.005
	FrameAgeBucketFactor = 2
	FrameAgeBuckets      = 12
)

// vehicleLabel names the vehicle a frame came from.
const vehicleLabel = "vehicle"

// FrameAge records how old each frame is when it reaches the GCS: from its
// capture, as the vehicle stamped it, to its arrival. The two clocks differ
// by however well the vehicle and the GCS's host are synchronized, so a
// frame can seem to arrive before it was captured; such frames are counted,
// not observed, so skew is visible without corrupting the histogram.
type FrameAge struct {
	age      *prometheus.HistogramVec
	negative *prometheus.CounterVec
}

// NewFrameAge creates the frame age metrics and registers them with reg.
func NewFrameAge(reg prometheus.Registerer) *FrameAge {
	f := &FrameAge{
		age: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "gcs_frame_age_seconds",
			Help:    "Time from a frame's capture, as its vehicle stamped it, to its arrival at the GCS.",
			Buckets: prometheus.ExponentialBuckets(FrameAgeFirstBucket, FrameAgeBucketFactor, FrameAgeBuckets),
		}, []string{vehicleLabel}),
		negative: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "gcs_frame_age_negative_total",
			Help: "Frames that arrived at the GCS before their capture time, by the clocks of the vehicle and the GCS's host.",
		}, []string{vehicleLabel}),
	}
	reg.MustRegister(f.age, f.negative)
	return f
}

// Observe records that frame, from vehicle, arrived at at. A frame without a
// capture time is not recorded.
func (f *FrameAge) Observe(vehicle string, frame *framepb.EncodedFrame, at time.Time) {
	if !frame.HasTimestamp() {
		return
	}
	age := at.Sub(frame.GetTimestamp().AsTime())
	if age < 0 {
		f.negative.WithLabelValues(vehicle).Inc()
		return
	}
	f.age.WithLabelValues(vehicle).Observe(age.Seconds())
}
