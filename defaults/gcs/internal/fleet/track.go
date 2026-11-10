package fleet

import (
	"math"

	telemetrypb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/telemetry"
)

const (
	// trackSpacing is how far, in meters, a vehicle must move from the last
	// point of its track for a new point to be added, so that a hovering
	// vehicle doesn't fill its track with one spot.
	trackSpacing = 3.0
	// trackLength is how much of the path flown, in meters, a track keeps:
	// the oldest points are dropped once the newer ones span it.
	trackLength = 200.0
	// earthRadius is the mean radius of the earth, in meters.
	earthRadius = 6_371_000.0
)

// track is where a vehicle has been: its recent positions, oldest first, as
// [long, lat, alt] triples, the order of GeoJSON coordinates. alt is meters
// above where the vehicle took off, as in VehicleJSON.Current.
type track struct {
	pts [][3]float64
}

// add appends the vehicle's position in t, unless t has no position fix or
// the vehicle is within trackSpacing of the last point, and drops the points
// older than trackLength of path.
func (tr *track) add(t *telemetrypb.Telemetry) {
	pos := t.GetPositionInfo()
	g := pos.GetGlobalPosition()
	if g == nil {
		return
	}
	lat, long := g.GetLatitude(), g.GetLongitude()
	// 0,0 is what a vehicle without a fix reports
	if (lat == 0 && long == 0) || math.IsNaN(lat) || math.IsNaN(long) {
		return
	}
	p := [3]float64{long, lat, math.Max(0, float64(pos.GetRelativePosition().GetZ()))}
	if n := len(tr.pts); n > 0 && distance(tr.pts[n-1], p) < trackSpacing {
		return
	}
	tr.pts = append(tr.pts, p)
	tr.trim()
}

// trim drops the oldest points while the rest still span trackLength, so the
// track spans at least trackLength, if flown, and at most one point more.
func (tr *track) trim() {
	length := 0.0
	// Walking back from the newest point, the first point whose path to the
	// newest spans trackLength is the oldest kept
	for i := len(tr.pts) - 1; i > 0; i-- {
		length += distance(tr.pts[i-1], tr.pts[i])
		if length >= trackLength {
			tr.pts = append(tr.pts[:0], tr.pts[i-1:]...)
			return
		}
	}
}

// points returns a copy of the track's points, or nil if it has none.
func (tr *track) points() [][3]float64 {
	if len(tr.pts) == 0 {
		return nil
	}
	return append([][3]float64(nil), tr.pts...)
}

// distance is the distance, in meters, between [long, lat, alt] points a and
// b, by the equirectangular approximation over the ground, which is close
// enough at the few meters it decides on.
func distance(a, b [3]float64) float64 {
	rad := math.Pi / 180
	x := (b[0] - a[0]) * rad * math.Cos((a[1]+b[1])/2*rad) * earthRadius
	y := (b[1] - a[1]) * rad * earthRadius
	return math.Sqrt(x*x + y*y + (b[2]-a[2])*(b[2]-a[2]))
}
