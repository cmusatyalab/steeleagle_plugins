package main

import (
	"math"
	"sync"
	"time"

	common "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/common"
	telemetrypb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/telemetry"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// Flight model constants. Distances are in meters, speeds in m/s.
const (
	ClimbRate    = 2
	DescentRate  = 1
	RTHSpeed     = 5
	MissionSide  = 50
	MissionSpeed = 5
	DMSTimeout   = 3 * time.Second

	// metersPerDegLat is the flat-earth meters per degree of latitude.
	metersPerDegLat = 111320.0
	// groundAltitude is the altitude above sea level of the takeoff point.
	groundAltitude = 300.0
	// missionAltitude is the altitude a mission climbs to if lower.
	missionAltitude = 10.0
	// batteryDrainPerSecond is 1% per minute airborne.
	batteryDrainPerSecond = 1.0 / 60
	satellites            = 12
)

// Mode is the vehicle's flight mode.
type Mode int

const (
	Landed Mode = iota
	TakingOff
	Flying
	Landing
	Returning
	Mission
)

func (m Mode) String() string {
	switch m {
	case Landed:
		return "Landed"
	case TakingOff:
		return "TakingOff"
	case Flying:
		return "Flying"
	case Landing:
		return "Landing"
	case Returning:
		return "Returning"
	case Mission:
		return "Mission"
	}
	return "Unknown"
}

// LatLon is a position in degrees.
type LatLon struct{ Lat, Lon float64 }

// offsetFrom returns p moved north and east by the given meters.
func offsetFrom(p LatLon, north, east float64) LatLon {
	return LatLon{
		Lat: p.Lat + north/metersPerDegLat,
		Lon: p.Lon + east/(metersPerDegLat*math.Cos(p.Lat*math.Pi/180)),
	}
}

// metersBetween returns the north and east meters from a to b.
func metersBetween(a, b LatLon) (north, east float64) {
	return (b.Lat - a.Lat) * metersPerDegLat,
		(b.Lon - a.Lon) * metersPerDegLat * math.Cos(a.Lat*math.Pi/180)
}

// Vehicle is a simulated vehicle. All methods are safe for concurrent use.
// Heading is in degrees clockwise from north; body-frame x is forward and y is
// right. The model never reads the wall clock: time comes in through Step and
// Telemetry.
type Vehicle struct {
	mu sync.Mutex

	name string
	home LatLon
	pos  LatLon
	alt  float64 // relative altitude, m
	// heading is in degrees clockwise from north, in [0, 360).
	heading float64
	mode    Mode

	targetAlt float64 // takeoff altitude target
	// vel is the commanded body-frame velocity (x, y, z m/s; yaw rate deg/s).
	vx, vy, vz, yawRate float64

	dmsArmed bool
	dmsSince time.Time // zero until the first Step after arming

	missionRunning bool
	legElapsed     time.Duration

	battery      float64
	gimbalPitch  float64
	curVx, curVy float64 // velocity actually flown in the last step
	curVz, curYr float64
}

// NewVehicle returns a landed vehicle at home with a full battery.
func NewVehicle(name string, home LatLon) *Vehicle {
	return &Vehicle{name: name, home: home, pos: home, mode: Landed, battery: 100}
}

// TakeOff climbs (or descends) to alt meters. It succeeds if already airborne.
func (v *Vehicle) TakeOff(alt float64) {
	v.mu.Lock()
	defer v.mu.Unlock()
	v.control()
	v.cancelMission()
	v.zeroVelocity()
	v.targetAlt = alt
	v.mode = TakingOff
}

// Land descends to the ground. It does nothing when already landed.
func (v *Vehicle) Land() {
	v.mu.Lock()
	defer v.mu.Unlock()
	v.control()
	v.cancelMission()
	v.zeroVelocity()
	if v.mode != Landed {
		v.mode = Landing
	}
}

// ReturnToHome flies home and lands. It does nothing when landed.
func (v *Vehicle) ReturnToHome() {
	v.mu.Lock()
	defer v.mu.Unlock()
	v.control()
	v.cancelMission()
	v.zeroVelocity()
	if v.mode != Landed {
		v.mode = Returning
	}
}

// Hold zeroes the velocity and cancels any mission or return.
func (v *Vehicle) Hold() {
	v.mu.Lock()
	defer v.mu.Unlock()
	v.control()
	v.cancelMission()
	v.zeroVelocity()
	if v.mode != Landed {
		v.mode = Flying
		v.targetAlt = v.alt
	}
}

// SetVelocity sets the body-frame velocity target in m/s and the yaw rate in
// deg/s, and arms the dead man's switch.
func (v *Vehicle) SetVelocity(x, y, z, yawRate float64) {
	v.mu.Lock()
	defer v.mu.Unlock()
	v.cancelMission()
	v.vx, v.vy, v.vz, v.yawRate = x, y, z, yawRate
	v.dmsArmed = true
	v.dmsSince = time.Time{}
	switch v.mode {
	case TakingOff, Landing, Returning, Mission:
		v.mode = Flying
	}
}

// SetGimbalPitch adds delta degrees to the gimbal pitch.
func (v *Vehicle) SetGimbalPitch(delta float64) {
	v.mu.Lock()
	defer v.mu.Unlock()
	v.control()
	v.gimbalPitch += delta
}

// StartMission starts the square pattern, climbing to altitude first if lower.
func (v *Vehicle) StartMission() {
	v.mu.Lock()
	defer v.mu.Unlock()
	v.control()
	v.zeroVelocity()
	v.missionRunning = true
	v.legElapsed = 0
	v.mode = Mission
}

// StopMission stops the pattern and reports whether one was running.
func (v *Vehicle) StopMission() (wasRunning bool) {
	v.mu.Lock()
	defer v.mu.Unlock()
	v.control()
	wasRunning = v.missionRunning
	v.cancelMission()
	return wasRunning
}

// MissionRunning reports whether the pattern is running.
func (v *Vehicle) MissionRunning() bool {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.missionRunning
}

// control disarms the dead man's switch. Callers hold v.mu.
func (v *Vehicle) control() {
	v.dmsArmed = false
	v.dmsSince = time.Time{}
}

// cancelMission ends the pattern, leaving the vehicle holding. Callers hold v.mu.
func (v *Vehicle) cancelMission() {
	if !v.missionRunning {
		return
	}
	v.missionRunning = false
	if v.mode == Mission {
		v.mode = Flying
		v.zeroVelocity()
	}
}

func (v *Vehicle) zeroVelocity() {
	v.vx, v.vy, v.vz, v.yawRate = 0, 0, 0, 0
	v.curVx, v.curVy, v.curVz, v.curYr = 0, 0, 0, 0
}

// move advances the position by a body-frame velocity over seconds.
func (v *Vehicle) move(x, y, seconds float64) {
	h := v.heading * math.Pi / 180
	north := x*math.Cos(h) - y*math.Sin(h)
	east := x*math.Sin(h) + y*math.Cos(h)
	v.pos = offsetFrom(v.pos, north*seconds, east*seconds)
}

func wrap360(d float64) float64 {
	d = math.Mod(d, 360)
	if d < 0 {
		d += 360
	}
	return d
}

// Step advances the model by dt. now is the time at the end of the step; it
// drives the dead man's switch.
func (v *Vehicle) Step(dt time.Duration, now time.Time) {
	v.mu.Lock()
	defer v.mu.Unlock()
	sec := dt.Seconds()

	if v.dmsArmed {
		if v.dmsSince.IsZero() {
			v.dmsSince = now.Add(-dt)
		}
		if now.Sub(v.dmsSince) >= DMSTimeout {
			v.control()
			v.zeroVelocity()
		}
	}

	v.curVx, v.curVy, v.curVz, v.curYr = 0, 0, 0, 0
	switch v.mode {
	case Landed:
		return
	case TakingOff:
		v.stepTakeOff(sec)
	case Flying:
		v.stepFlying(sec)
	case Landing:
		v.stepLanding(sec)
	case Returning:
		v.stepReturning(sec)
	case Mission:
		v.stepMission(dt)
	}
	v.battery = math.Max(0, v.battery-batteryDrainPerSecond*sec)
}

func (v *Vehicle) stepTakeOff(sec float64) {
	diff := v.targetAlt - v.alt
	step := ClimbRate * sec
	if math.Abs(diff) <= step {
		v.alt = v.targetAlt
		v.mode = Flying
		return
	}
	v.alt += math.Copysign(step, diff)
	v.curVz = math.Copysign(ClimbRate, diff)
}

func (v *Vehicle) stepFlying(sec float64) {
	v.move(v.vx, v.vy, sec)
	v.heading = wrap360(v.heading + v.yawRate*sec)
	v.alt = math.Max(0, v.alt+v.vz*sec)
	v.curVx, v.curVy, v.curVz, v.curYr = v.vx, v.vy, v.vz, v.yawRate
}

func (v *Vehicle) stepLanding(sec float64) {
	step := DescentRate * sec
	if v.alt <= step {
		v.alt = 0
		v.mode = Landed
		return
	}
	v.alt -= step
	v.curVz = -DescentRate
}

func (v *Vehicle) stepReturning(sec float64) {
	north, east := metersBetween(v.pos, v.home)
	dist := math.Hypot(north, east)
	step := RTHSpeed * sec
	if dist <= step {
		v.pos = v.home
		v.mode = Landing
		v.stepLanding(sec)
		return
	}
	v.heading = wrap360(math.Atan2(east, north) * 180 / math.Pi)
	v.move(RTHSpeed, 0, sec)
	v.curVx = RTHSpeed
}

// stepMission flies the square: MissionSide meters per leg at MissionSpeed,
// turning 90 degrees right at each corner.
func (v *Vehicle) stepMission(dt time.Duration) {
	leg := time.Duration(MissionSide / float64(MissionSpeed) * float64(time.Second))
	if v.alt < missionAltitude {
		v.alt = math.Min(missionAltitude, v.alt+ClimbRate*dt.Seconds())
		v.curVz = ClimbRate
	}
	remaining := dt
	for remaining > 0 {
		span := min(remaining, leg-v.legElapsed)
		v.move(MissionSpeed, 0, span.Seconds())
		v.legElapsed += span
		remaining -= span
		if v.legElapsed >= leg {
			v.legElapsed = 0
			v.heading = wrap360(v.heading + 90)
		}
	}
	v.curVx = MissionSpeed
}

func (v *Vehicle) telemetryMode() telemetrypb.Mode {
	switch v.mode {
	case TakingOff:
		return telemetrypb.Mode_MODE_TAKEOFF
	case Flying, Mission:
		return telemetrypb.Mode_MODE_GUIDED
	case Landing:
		return telemetrypb.Mode_MODE_LAND
	case Returning:
		return telemetrypb.Mode_MODE_RETURN_TO_HOME
	}
	return telemetrypb.Mode_MODE_SIT
}

// Telemetry returns a snapshot of the vehicle stamped at now.
func (v *Vehicle) Telemetry(now time.Time) *telemetrypb.Telemetry {
	v.mu.Lock()
	defer v.mu.Unlock()
	north, east := metersBetween(v.home, v.pos)
	status := telemetrypb.MotionStatus_MOTION_STATUS_HOLDING
	if v.mode == Landed {
		status = telemetrypb.MotionStatus_MOTION_STATUS_STOPPED
	} else if v.curVx != 0 || v.curVy != 0 || v.curVz != 0 {
		status = telemetrypb.MotionStatus_MOTION_STATUS_IN_TRANSIT
	}
	return telemetrypb.Telemetry_builder{
		Timestamp:   timestamppb.New(now),
		BatteryInfo: telemetrypb.BatteryInfo_builder{Percentage: proto.Uint32(uint32(math.Ceil(v.battery)))}.Build(),
		GpsInfo:     telemetrypb.GpsInfo_builder{Satellites: proto.Uint32(satellites)}.Build(),
		AlertInfo: telemetrypb.AlertInfo_builder{
			MagnetometerWarning: telemetrypb.AlertInfo_MAGNETOMETER_WARNING_UNSPECIFIED,
		}.Build(),
		PositionInfo: telemetrypb.PositionInfo_builder{
			Home: common.GlobalPosition_builder{
				Latitude: proto.Float64(v.home.Lat), Longitude: proto.Float64(v.home.Lon),
				Altitude: proto.Float32(groundAltitude),
			}.Build(),
			GlobalPosition: common.GlobalPosition_builder{
				Latitude: proto.Float64(v.pos.Lat), Longitude: proto.Float64(v.pos.Lon),
				Altitude: proto.Float32(float32(groundAltitude + v.alt)),
				Heading:  proto.Float32(float32(v.heading)),
			}.Build(),
			RelativePosition: common.RelativePosition_builder{
				X: proto.Float32(float32(north)), Y: proto.Float32(float32(east)),
				Z: proto.Float32(float32(v.alt)),
			}.Build(),
			VelocityBody: common.Velocity_builder{
				XVel: proto.Float32(float32(v.curVx)), YVel: proto.Float32(float32(v.curVy)),
				ZVel: proto.Float32(float32(v.curVz)), AngularVel: proto.Float32(float32(v.curYr)),
			}.Build(),
		}.Build(),
		GimbalInfo: telemetrypb.GimbalInfo_builder{
			PoseBody: common.Pose_builder{Pitch: proto.Float32(float32(v.gimbalPitch))}.Build(),
		}.Build(),
		Mode:         v.telemetryMode().Enum(),
		MotionStatus: status.Enum(),
	}.Build()
}
