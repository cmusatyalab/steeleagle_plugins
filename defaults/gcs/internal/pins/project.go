// Package pins places engines' detections on the map: each box is projected
// from the camera that saw it onto the ground, and repeated sightings of the
// same object are grouped into one pin, held in memory until it expires.
package pins

import "math"

// MaxRangeM is the farthest along the ground a detection is placed: a ray
// that meets the ground farther away, near the horizon, is too uncertain.
// It matches the map's camera line (CAMERA_MAX_RANGE_M in mapOverlays.js).
const MaxRangeM = 150.0

const (
	earthRadiusM = 6371000.0
	rad          = math.Pi / 180
)

// CameraPose is where a camera was and where it pointed.
type CameraPose struct {
	Lat, Lon float64
	// Alt is in metres above takeoff, where the ground is taken to be.
	Alt float64
	// Yaw is in degrees clockwise from north.
	Yaw float64
	// Pitch is in degrees, negative below the horizon.
	Pitch float64
}

// FOV is a camera's field of view, in degrees.
type FOV struct{ H, V float64 }

// Project returns where the ray from camera p through the image point (cx,
// cy) meets the ground, taken as flat at the takeoff altitude and the camera
// as having no roll. cx and cy are normalised: 0..1, x right and y down. It
// fails if the camera is on the ground, has no field of view, or the ray
// doesn't meet the ground within MaxRangeM.
func Project(p CameraPose, fov FOV, cx, cy float64) (lat, lon float64, ok bool) {
	if fov.H <= 0 || fov.V <= 0 || p.Alt <= 0 {
		return 0, 0, false
	}
	pitch := p.Pitch * rad
	th, tv := math.Tan(fov.H/2*rad), math.Tan(fov.V/2*rad)
	up, side := 1-2*cy, 2*cx-1
	fwd := math.Cos(pitch) - tv*up*math.Sin(pitch)
	right := th * side
	vert := math.Sin(pitch) + tv*up*math.Cos(pitch)
	if vert >= 0 {
		return 0, 0, false
	}
	k := p.Alt / -vert
	if k*math.Hypot(fwd, right) > MaxRangeM {
		return 0, 0, false
	}
	x, y := k*fwd, k*right
	yaw := p.Yaw * rad
	north := x*math.Cos(yaw) - y*math.Sin(yaw)
	east := x*math.Sin(yaw) + y*math.Cos(yaw)
	lat, lon = offset(p.Lat, p.Lon, north, east)
	return lat, lon, true
}

// offset returns the point north and east metres from (lat, lon), by the
// equirectangular approximation.
func offset(lat, lon, north, east float64) (float64, float64) {
	return lat + north/earthRadiusM/rad, lon + east/(earthRadiusM*math.Cos(lat*rad))/rad
}

// groundDistance is the distance in metres between two points along the
// ground, by the equirectangular approximation.
func groundDistance(aLat, aLon, bLat, bLon float64) float64 {
	north := (bLat - aLat) * rad * earthRadiusM
	east := (bLon - aLon) * rad * earthRadiusM * math.Cos((aLat+bLat)/2*rad)
	return math.Hypot(north, east)
}
