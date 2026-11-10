package fleet

import (
	"math"
	"testing"
	"time"

	common "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/common"
	telemetrypb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/telemetry"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// at builds telemetry with the vehicle at lat, long, on the ground.
func at(lat, long float64) *telemetrypb.Telemetry {
	return atAlt(lat, long, 0)
}

// atAlt builds telemetry with the vehicle at lat, long, z meters above where
// it took off.
func atAlt(lat, long float64, z float32) *telemetrypb.Telemetry {
	return telemetrypb.Telemetry_builder{
		Timestamp: timestamppb.New(time.Now()),
		PositionInfo: telemetrypb.PositionInfo_builder{
			GlobalPosition: common.GlobalPosition_builder{
				Latitude: proto.Float64(lat), Longitude: proto.Float64(long),
			}.Build(),
			RelativePosition: common.RelativePosition_builder{Z: proto.Float32(z)}.Build(),
		}.Build(),
	}.Build()
}

// metersLat is roughly a meter of latitude, in degrees.
const metersLat = 1 / 111_195.0

func TestTrackRecordsFirstPosition(t *testing.T) {
	var tr track
	tr.add(at(40.2, -79.8))
	got := tr.points()
	if len(got) != 1 || got[0] != [3]float64{-79.8, 40.2, 0} {
		t.Fatalf("points = %v, want [[-79.8 40.2 0]] as [long, lat, alt]", got)
	}
}

func TestTrackRecordsAltitude(t *testing.T) {
	var tr track
	tr.add(atAlt(40.2, -79.8, 25))
	tr.add(atAlt(40.2, -79.8, -4))
	got := tr.points()
	if len(got) != 2 || got[0][2] != 25 || got[1][2] != 0 {
		t.Fatalf("points = %v, want alts 25 then 0: below takeoff clamps to 0, like current.alt", got)
	}
}

func TestTrackAddsClimbInPlace(t *testing.T) {
	var tr track
	tr.add(atAlt(40.2, -79.8, 0))
	tr.add(atAlt(40.2, -79.8, trackSpacing/2))
	tr.add(atAlt(40.2, -79.8, 2*trackSpacing))
	if got := tr.points(); len(got) != 2 {
		t.Fatalf("points = %v, want 2: a climb of twice the spacing is movement too", got)
	}
}

func TestTrackSkipsPositionsCloserThanSpacing(t *testing.T) {
	var tr track
	tr.add(at(40.2, -79.8))
	tr.add(at(40.2+(trackSpacing/2)*metersLat, -79.8))
	if got := tr.points(); len(got) != 1 {
		t.Fatalf("points = %v, want only the first: the vehicle moved half the spacing", got)
	}
}

func TestTrackAddsPositionsBeyondSpacing(t *testing.T) {
	var tr track
	tr.add(at(40.2, -79.8))
	// Moves in small steps, each under the spacing: the distance is measured
	// from the last point kept, so the steps add up
	step := (trackSpacing / 3) * metersLat
	for i := 1; i <= 4; i++ {
		tr.add(at(40.2+float64(i)*step, -79.8))
	}
	if got := tr.points(); len(got) != 2 {
		t.Fatalf("points = %v, want 2: four steps of a third of the spacing cross it once", got)
	}
}

func TestTrackKeepsOnlyTrackLength(t *testing.T) {
	var tr track
	// Meters between points, not dividing trackLength, so no point sits
	// exactly at the edge
	step := 7.0
	n := int(2 * trackLength / step)
	for i := range n {
		tr.add(at(40+float64(i)*step*metersLat, -79.8))
	}
	got := tr.points()
	// The fewest newest points that span trackLength
	if want := int(math.Ceil(trackLength/step)) + 1; len(got) != want {
		t.Fatalf("len = %d, want %d: the fewest newest points spanning %v m", len(got), want, trackLength)
	}
	if want := 40 + float64(n-1)*step*metersLat; got[len(got)-1][1] != want {
		t.Errorf("newest lat = %v, want %v", got[len(got)-1][1], want)
	}
}

func TestTrackKeepsLengthWithSparsePoints(t *testing.T) {
	var tr track
	// Points further apart than trackLength, as when telemetry stalls: the
	// track keeps the last two, the segment the vehicle is on
	far := 2 * trackLength * metersLat
	for i := range 4 {
		tr.add(at(40+float64(i)*far, -79.8))
	}
	if got := tr.points(); len(got) != 2 {
		t.Fatalf("points = %v, want the last 2", got)
	}
}

func TestTrackSkipsMissingFix(t *testing.T) {
	var tr track
	tr.add(telemetrypb.Telemetry_builder{Timestamp: timestamppb.New(time.Now())}.Build())
	tr.add(at(0, 0))
	if got := tr.points(); len(got) != 0 {
		t.Fatalf("points = %v, want none: no global position, then 0,0 (no fix)", got)
	}
}

func TestTrackPointsIsACopy(t *testing.T) {
	var tr track
	tr.add(at(40.2, -79.8))
	got := tr.points()
	got[0][0] = 0
	if tr.points()[0][0] != -79.8 {
		t.Fatal("changing the returned points changed the track")
	}
}

func TestFlowsTrack(t *testing.T) {
	_, f, _ := flowsFixture(t)
	if got := f.Track(v1); got != nil {
		t.Fatalf("Track before telemetry = %v, want nil", got)
	}
	f.ReceivedTelemetry(v1, at(40.2, -79.8))
	f.ReceivedTelemetry(v1, at(40.2+2*trackSpacing*metersLat, -79.8))
	if got := f.Track(v1); len(got) != 2 {
		t.Fatalf("Track = %v, want 2 points", got)
	}
}

func TestVehiclesEndpointTrack(t *testing.T) {
	_, f, h := vehiclesFixture(t)
	_, out, body := getVehicles(t, h)
	f.Wait()
	if len(out) != 2 || len(out[0].Track) != 1 || out[0].Track[0] != [3]float64{-79.8, 40.2, 10} {
		t.Fatalf("body %s: want each vehicle's track, as [long, lat, alt]", body)
	}
}
