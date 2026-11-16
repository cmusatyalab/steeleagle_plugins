package pins

import (
	"math"
	"testing"
)

const testLat, testLon = 40.4433, -79.9436

// metres is how far north and east b is from a, by the equirectangular
// approximation.
func metres(aLat, aLon, bLat, bLon float64) (north, east float64) {
	const r = 6371000.0
	rad := math.Pi / 180
	return (bLat - aLat) * rad * r, (bLon - aLon) * rad * r * math.Cos(aLat*rad)
}

func pose(alt, yaw, pitch float64) CameraPose {
	return CameraPose{Lat: testLat, Lon: testLon, Alt: alt, Yaw: yaw, Pitch: pitch}
}

var testFOV = FOV{H: 69, V: 42}

func TestProjectStraightDownCentre(t *testing.T) {
	lat, lon, ok := Project(pose(20, 0, -90), testFOV, 0.5, 0.5)
	if !ok {
		t.Fatal("not ok")
	}
	n, e := metres(testLat, testLon, lat, lon)
	if math.Hypot(n, e) > 0.1 {
		t.Fatalf("got %.2f m north, %.2f m east; want under the vehicle", n, e)
	}
}

func TestProjectPitch45Centre(t *testing.T) {
	lat, lon, ok := Project(pose(20, 90, -45), testFOV, 0.5, 0.5)
	if !ok {
		t.Fatal("not ok")
	}
	n, e := metres(testLat, testLon, lat, lon)
	if math.Abs(e-20) > 0.2 || math.Abs(n) > 0.2 {
		t.Fatalf("got %.2f m north, %.2f m east; want 20 m east", n, e)
	}
}

func TestProjectRightEdgeTurnsRight(t *testing.T) {
	cLat, cLon, _ := Project(pose(20, 0, -45), testFOV, 0.5, 0.5)
	rLat, rLon, ok := Project(pose(20, 0, -45), testFOV, 1, 0.5)
	if !ok {
		t.Fatal("not ok")
	}
	_, ce := metres(testLat, testLon, cLat, cLon)
	_, re := metres(testLat, testLon, rLat, rLon)
	if re <= ce+1 {
		t.Fatalf("right edge %.2f m east, centre %.2f m east; want right edge further east", re, ce)
	}
}

func TestProjectBottomIsNearer(t *testing.T) {
	tLat, tLon, ok1 := Project(pose(20, 0, -45), testFOV, 0.5, 0)
	bLat, bLon, ok2 := Project(pose(20, 0, -45), testFOV, 0.5, 1)
	if !ok1 || !ok2 {
		t.Fatal("not ok")
	}
	tn, _ := metres(testLat, testLon, tLat, tLon)
	bn, _ := metres(testLat, testLon, bLat, bLon)
	if bn >= tn {
		t.Fatalf("bottom %.2f m, top %.2f m; want bottom nearer", bn, tn)
	}
}

func TestProjectBeyondRange(t *testing.T) {
	if _, _, ok := Project(pose(5, 0, -2), testFOV, 0.5, 0.5); !ok {
		t.Error("pitch -2 at 5 m (~143 m) should be in range")
	}
	if _, _, ok := Project(pose(5, 0, -1), testFOV, 0.5, 0.5); ok {
		t.Error("pitch -1 at 5 m (~286 m) should be out of range")
	}
}

func TestProjectAboveHorizon(t *testing.T) {
	if _, _, ok := Project(pose(20, 0, 0), testFOV, 0.5, 0.5); ok {
		t.Error("level centre ray should miss")
	}
	if _, _, ok := Project(pose(20, 0, 10), testFOV, 0.5, 0.5); ok {
		t.Error("upward ray should miss")
	}
}

func TestProjectZeroFOV(t *testing.T) {
	if _, _, ok := Project(pose(20, 0, -45), FOV{}, 0.5, 0.5); ok {
		t.Error("no FOV should not project")
	}
}

func TestProjectOnGround(t *testing.T) {
	if _, _, ok := Project(pose(0, 0, -45), testFOV, 0.5, 0.5); ok {
		t.Error("a camera on the ground should not project")
	}
}
