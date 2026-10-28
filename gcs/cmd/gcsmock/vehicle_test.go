package main

import (
	"math"
	"testing"
	"time"

	telemetrypb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/telemetry"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
)

var (
	testHome  = LatLon{Lat: 40.4433, Lon: -79.9436}
	testEpoch = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
)

// clock drives a vehicle with a synthetic clock.
type clock struct {
	v   *Vehicle
	now time.Time
}

func newClock(v *Vehicle) *clock { return &clock{v: v, now: testEpoch} }

// run steps the vehicle in 100 ms increments for d.
func (c *clock) run(d time.Duration) {
	const dt = 100 * time.Millisecond
	for n := int(d / dt); n > 0; n-- {
		c.now = c.now.Add(dt)
		c.v.Step(dt, c.now)
	}
}

// offset returns the vehicle's position in meters (north, east) from home.
func offset(v *Vehicle, home LatLon) (north, east float64) {
	p := v.Telemetry(testEpoch).GetPositionInfo().GetGlobalPosition()
	north = (p.GetLatitude() - home.Lat) * metersPerDegLat
	east = (p.GetLongitude() - home.Lon) * metersPerDegLat * math.Cos(home.Lat*math.Pi/180)
	return
}

func relAlt(v *Vehicle) float64 {
	return float64(v.Telemetry(testEpoch).GetPositionInfo().GetRelativePosition().GetZ())
}

func velX(v *Vehicle) float64 {
	return float64(v.Telemetry(testEpoch).GetPositionInfo().GetVelocityBody().GetXVel())
}

// flying returns a vehicle airborne at alt meters.
func flying(alt float64) (*Vehicle, *clock) {
	v := NewVehicle("alpha", testHome)
	c := newClock(v)
	v.TakeOff(alt)
	c.run(time.Duration(alt/ClimbRate*float64(time.Second)) + time.Second)
	return v, c
}

func TestTakeOff(t *testing.T) {
	v := NewVehicle("alpha", testHome)
	c := newClock(v)
	if v.mode != Landed {
		t.Fatalf("initial mode = %v", v.mode)
	}
	v.TakeOff(10)
	c.run(6 * time.Second)
	if got := relAlt(v); math.Abs(got-10) > 0.01 {
		t.Errorf("altitude = %v, want 10", got)
	}
	if v.mode != Flying {
		t.Errorf("mode = %v, want Flying", v.mode)
	}
}

func TestLand(t *testing.T) {
	v, c := flying(10)
	v.Land()
	c.run(11 * time.Second)
	if got := relAlt(v); math.Abs(got) > 0.01 {
		t.Errorf("altitude = %v, want 0", got)
	}
	if v.mode != Landed {
		t.Errorf("mode = %v, want Landed", v.mode)
	}
}

func TestVelocityHeading(t *testing.T) {
	v, c := flying(10)
	v.heading = 90
	v.SetVelocity(5, 0, 0, 0)
	c.run(time.Second)
	n, e := offset(v, testHome)
	if math.Abs(e-5) > 0.5 || math.Abs(n) > 0.5 {
		t.Errorf("offset north=%v east=%v, want 0, 5", n, e)
	}
}

func TestVelocityRightAndYaw(t *testing.T) {
	v, c := flying(10)
	v.SetVelocity(0, 5, 0, 90) // right of north is east; yaw 90 deg/s
	c.run(time.Second)
	if math.Abs(v.heading-90) > 1 {
		t.Errorf("heading = %v, want ~90", v.heading)
	}
	n, e := offset(v, testHome)
	if e < 3 || math.Abs(n) > 3 {
		t.Errorf("offset north=%v east=%v, want mostly east", n, e)
	}
}

func TestDMS(t *testing.T) {
	v, c := flying(10)
	v.SetVelocity(5, 0, 0, 0)
	c.run(3100 * time.Millisecond)
	if got := velX(v); got != 0 {
		t.Errorf("velocity at 3.1 s = %v, want 0", got)
	}
	n0, e0 := offset(v, testHome)
	c.run(2 * time.Second)
	n1, e1 := offset(v, testHome)
	if n0 != n1 || e0 != e1 {
		t.Errorf("position changed after hold: (%v,%v) -> (%v,%v)", n0, e0, n1, e1)
	}
}

func TestDMSDisarmedByControlCall(t *testing.T) {
	v, c := flying(10)
	v.SetVelocity(5, 0, 0, 0)
	c.run(2 * time.Second)
	v.SetGimbalPitch(-10)
	c.run(3 * time.Second)
	if got := velX(v); got != 5 {
		t.Errorf("velocity after control call = %v, want 5", got)
	}
}

func TestDMSRearmedBySetVelocity(t *testing.T) {
	v, c := flying(10)
	v.SetVelocity(5, 0, 0, 0)
	c.run(2 * time.Second)
	v.SetVelocity(5, 0, 0, 0)
	c.run(2 * time.Second)
	if got := velX(v); got != 5 {
		t.Errorf("velocity = %v, want 5 (re-armed)", got)
	}
	c.run(time.Second + 200*time.Millisecond)
	if got := velX(v); got != 0 {
		t.Errorf("velocity = %v, want 0 after 3 s", got)
	}
}

func TestRTH(t *testing.T) {
	v, c := flying(5)
	v.heading = 90
	v.SetVelocity(5, 0, 0, 0)
	c.run(2900 * time.Millisecond)
	v.Hold()
	v.pos = offsetFrom(testHome, 0, 100)
	v.ReturnToHome()
	c.run(30 * time.Second)
	n, e := offset(v, testHome)
	if math.Hypot(n, e) > 1 {
		t.Errorf("distance from home = %v, want < 1", math.Hypot(n, e))
	}
	if v.mode != Landed {
		t.Errorf("mode = %v, want Landed", v.mode)
	}
}

func TestMissionSquare(t *testing.T) {
	v, c := flying(10)
	v.StartMission()
	c.run(40 * time.Second)
	n, e := offset(v, testHome)
	if math.Hypot(n, e) > 60 {
		t.Errorf("distance from home = %v, want <= 60", math.Hypot(n, e))
	}
	if v.mode != Mission {
		t.Errorf("mode = %v, want Mission", v.mode)
	}
	if !v.StopMission() {
		t.Error("first StopMission = false, want true")
	}
	if v.StopMission() {
		t.Error("second StopMission = true, want false")
	}
}

func TestHoldCancelsMission(t *testing.T) {
	v, c := flying(10)
	v.StartMission()
	c.run(5 * time.Second)
	v.Hold()
	if v.StopMission() {
		t.Error("StopMission after Hold = true, want false")
	}
	if velX(v) != 0 {
		t.Errorf("velocity after Hold = %v", velX(v))
	}
}

func TestBatteryDrain(t *testing.T) {
	v, c := flying(10)
	c.run(60 * time.Second)
	got := v.Telemetry(c.now).GetBatteryInfo().GetPercentage()
	if got > 99 || got < 97 {
		t.Errorf("battery after ~65 s airborne = %d, want ~99", got)
	}
}

func TestTelemetryFields(t *testing.T) {
	v, c := flying(10)
	v.SetGimbalPitch(-30)
	v.heading = 450 // wraps
	v.SetVelocity(2, 1, 0.5, 10)
	c.run(500 * time.Millisecond)
	tel := v.Telemetry(c.now)
	j := fleet.ToVehicleJSON(fleet.Vehicle{Name: "alpha", Plugin: "mock"}, tel, "", c.now.Add(2*time.Second))

	if j.LastUpdated < 1.99 || j.LastUpdated > 2.01 {
		t.Errorf("last_updated = %v, want 2", j.LastUpdated)
	}
	if j.Battery < 99 || j.Battery > 100 {
		t.Errorf("battery = %d", j.Battery)
	}
	if j.Sats != 12 {
		t.Errorf("sats = %d, want 12", j.Sats)
	}
	if j.Home == nil || j.Home.Lat != testHome.Lat || j.Home.Long != testHome.Lon {
		t.Errorf("home = %+v", j.Home)
	}
	if j.Current.Lat == 0 || j.Current.Long == 0 {
		t.Errorf("current = %+v", j.Current)
	}
	if math.Abs(j.Current.Alt-10) > 0.5 {
		t.Errorf("current alt = %v, want ~10", j.Current.Alt)
	}
	if j.Bearing < 0 || j.Bearing >= 360 {
		t.Errorf("bearing = %v", j.Bearing)
	}
	if j.Velocity == nil || j.Velocity.XVel != 2 || j.Velocity.YVel != 1 || j.Velocity.ZVel != 0.5 || j.Velocity.AngularVel != 10 {
		t.Errorf("velocity = %+v", j.Velocity)
	}
	if got := tel.GetPositionInfo().GetGlobalPosition().GetAltitude(); math.Abs(float64(got)-(300+relAlt(v))) > 0.01 {
		t.Errorf("global altitude = %v, want 300+rel", got)
	}
	if got := tel.GetPositionInfo().GetHome().GetAltitude(); got != 300 {
		t.Errorf("home altitude = %v, want 300", got)
	}
	if got := tel.GetGimbalInfo().GetPoseBody().GetPitch(); got != -30 {
		t.Errorf("gimbal pitch = %v", got)
	}
	if tel.GetMode() != telemetrypb.Mode_MODE_GUIDED {
		t.Errorf("mode = %v, want GUIDED", tel.GetMode())
	}
	if !tel.HasMode() || !tel.HasTimestamp() {
		t.Error("mode or timestamp unset")
	}
}

func TestTelemetryModes(t *testing.T) {
	v := NewVehicle("alpha", testHome)
	want := func(m telemetrypb.Mode) {
		t.Helper()
		if got := v.Telemetry(testEpoch).GetMode(); got != m {
			t.Errorf("mode = %v, want %v", got, m)
		}
	}
	want(telemetrypb.Mode_MODE_SIT)
	v.TakeOff(10)
	want(telemetrypb.Mode_MODE_TAKEOFF)
}
