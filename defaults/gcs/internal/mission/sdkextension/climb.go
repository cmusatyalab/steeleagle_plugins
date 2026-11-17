package gcsactions

import (
	"fmt"
	"math"
	"time"

	"github.com/cmusatyalab/steeleagle/sdk"
	"github.com/cmusatyalab/steeleagle/sdk/dsl"
	"github.com/cmusatyalab/steeleagle/sdk/enums"
	"github.com/cmusatyalab/steeleagle/sdk/opt"
)

const (
	maxTelemetryAge = 2 * time.Second
	climbTimeout    = time.Minute
	climbTolerance  = 0.2 // meters
)

// ClimbToAltitude climbs to the requested height above the takeoff point.
// It keeps the current horizontal position and heading and stops once the
// target altitude is reached. It does not descend if already above target.
type ClimbToAltitude struct {
	Altitude float32 // target height above takeoff, in meters
}

var _ dsl.Action = (*ClimbToAltitude)(nil)

func (a *ClimbToAltitude) Execute(v sdk.Vehicle, _ dsl.MissionData) error {
	if !finite(a.Altitude) || a.Altitude <= 0 {
		return fmt.Errorf("climb altitude must be a finite, positive number of meters above takeoff")
	}
	_, current, err := currentAltitude(v)
	if err != nil {
		return err
	}
	if current >= a.Altitude {
		return nil
	}
	delta := a.Altitude - current
	if !finite(delta) {
		return fmt.Errorf("climb displacement is outside the supported numeric range")
	}

	// The V4 call sends its RPC before returning the waiter. Check reports
	// an immediate dispatch error. The SDK's relative-position waiter
	// compares the requested displacement with home-relative telemetry.
	// Check arrival against the actual target altitude instead.
	issued := time.Now()
	frame := opt.WithReferenceFrame[opt.SetRelativePositionTargetOption](enums.ReferenceFrameBody)
	if _, err := v.SetRelativePositionTarget(0, 0, delta, 0, frame).Check(); err != nil {
		return fmt.Errorf("commanding climb to %g m: %w", a.Altitude, err)
	}

	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	timeout := time.NewTimer(climbTimeout)
	defer timeout.Stop()
	for {
		select {
		case <-v.Ctx().Done():
			return v.Ctx().Err()
		case <-timeout.C:
			return fmt.Errorf("timed out waiting to reach %g m above takeoff", a.Altitude)
		case <-ticker.C:
			sample, altitude, err := currentAltitude(v)
			if err != nil {
				return err
			}
			timestamp, _ := sample.GetTimestamp() // already checked by currentAltitude
			if timestamp.AsTime().Before(issued) {
				continue
			}
			motion, err := sample.GetMotionStatus()
			if err != nil {
				return fmt.Errorf("reading motion status during climb: %w", err)
			}
			if motion == enums.MotionStatusHolding && math.Abs(float64(altitude-a.Altitude)) <= climbTolerance {
				return nil
			}
		}
	}
}

func currentAltitude(v sdk.Vehicle) (sdk.Telemetry, float32, error) {
	samples, err := v.GetTelemetry().Wait()
	if err != nil {
		return nil, 0, fmt.Errorf("reading climb telemetry: %w", err)
	}
	if len(samples) == 0 {
		return nil, 0, fmt.Errorf("climb requires current relative-altitude telemetry")
	}
	sample := samples[len(samples)-1]
	stamp, err := sample.GetTimestamp()
	if err != nil || !stamp.IsValid() {
		return nil, 0, fmt.Errorf("climb requires telemetry with a valid timestamp")
	}
	if age := time.Since(stamp.AsTime()); age < 0 || age > maxTelemetryAge {
		return nil, 0, fmt.Errorf("climb requires fresh telemetry, sample age is %s", age.Round(time.Millisecond))
	}
	position, err := sample.GetPositionInfo()
	if err != nil {
		return nil, 0, fmt.Errorf("reading climb position telemetry: %w", err)
	}
	relative, err := position.GetRelativePosition()
	if err != nil {
		return nil, 0, fmt.Errorf("reading climb relative position: %w", err)
	}
	altitude, err := relative.GetZ()
	if err != nil {
		return nil, 0, fmt.Errorf("reading climb relative altitude: %w", err)
	}
	if !finite(altitude) {
		return nil, 0, fmt.Errorf("climb requires finite relative-altitude telemetry")
	}
	return sample, altitude, nil
}

func finite(value float32) bool {
	return !math.IsNaN(float64(value)) && !math.IsInf(float64(value), 0)
}
