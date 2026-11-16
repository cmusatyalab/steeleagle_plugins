package pins

import (
	"math"
	"reflect"
	"testing"
	"time"
)

var base = time.Date(2026, 11, 12, 12, 0, 0, 0, time.UTC)

func at(sec float64) time.Time { return base.Add(time.Duration(sec * float64(time.Second))) }

// east returns the point m metres east of the test origin.
func east(m float64) (float64, float64) { return offset(testLat, testLon, 0, m) }

// hit is a car seen by alpha's camera in frame seq, m metres east of the
// origin, at sec.
func hit(seq uint64, m, sec float64) Hit {
	lat, lon := east(m)
	return Hit{Engine: "det", Class: "car", Vehicle: "alpha", Producer: "alpha.rgb", Seq: seq, Lat: lat, Lon: lon, At: at(sec)}
}

func TestStoreMinSightingsPromotes(t *testing.T) {
	s := NewStore()
	s.Add(hit(1, 0, 0))
	s.Add(hit(2, 0, 1))
	if got := s.Snapshot(at(1)); len(got) != 0 {
		t.Fatalf("after 2 frames got %d pins, want 0", len(got))
	}
	s.Add(hit(3, 0, 2))
	got := s.Snapshot(at(2))
	if len(got) != 1 || got[0].Sightings != 3 {
		t.Fatalf("after 3 frames got %+v, want 1 pin with 3 sightings", got)
	}
}

func TestStoreWindowIsFiveSeconds(t *testing.T) {
	s := NewStore()
	s.Add(hit(1, 0, 0))
	s.Add(hit(2, 0, 3))
	s.Add(hit(3, 0, 6))
	if got := s.Snapshot(at(6)); len(got) != 0 {
		t.Fatalf("frames at 0, 3, 6 s gave %d pins, want 0", len(got))
	}
	s.Add(hit(4, 0, 7))
	if got := s.Snapshot(at(7)); len(got) != 1 {
		t.Fatalf("frames at 3, 6, 7 s gave %d pins, want 1", len(got))
	}
}

func TestStoreBoxesInOneFrameCountOnce(t *testing.T) {
	s := NewStore()
	s.Add(hit(1, 0, 0))
	s.Add(hit(1, 1, 0))
	s.Add(hit(1, 2, 0))
	if got := s.Snapshot(at(0)); len(got) != 0 {
		t.Fatalf("3 boxes in one frame gave %d pins, want 0", len(got))
	}
	s.Add(hit(2, 0, 1))
	s.Add(hit(3, 0, 2))
	got := s.Snapshot(at(2))
	if len(got) != 1 || got[0].Sightings != 3 {
		t.Fatalf("got %+v, want 1 pin seen in 3 frames", got)
	}
}

func seen(s *Store, h func(seq uint64, sec float64) Hit) {
	for i := range 3 {
		s.Add(h(uint64(i+1), float64(i)))
	}
}

func TestStoreGroupsWithinRadius(t *testing.T) {
	s := NewStore()
	seen(s, func(seq uint64, sec float64) Hit { return hit(seq, 0, sec) })
	seen(s, func(seq uint64, sec float64) Hit { return hit(seq+10, 3, sec) })
	if got := s.Snapshot(at(3)); len(got) != 1 {
		t.Fatalf("hits 3 m apart gave %d pins, want 1", len(got))
	}
	s = NewStore()
	seen(s, func(seq uint64, sec float64) Hit { return hit(seq, 0, sec) })
	seen(s, func(seq uint64, sec float64) Hit { return hit(seq+10, 8, sec) })
	if got := s.Snapshot(at(3)); len(got) != 2 {
		t.Fatalf("hits 8 m apart gave %d pins, want 2", len(got))
	}
}

func TestStoreClassChangeIsNewPin(t *testing.T) {
	s := NewStore()
	seen(s, func(seq uint64, sec float64) Hit { return hit(seq, 0, sec) })
	seen(s, func(seq uint64, sec float64) Hit { h := hit(seq, 0, sec); h.Class = "truck"; return h })
	if got := s.Snapshot(at(3)); len(got) != 2 {
		t.Fatalf("car and truck gave %d pins, want 2", len(got))
	}
}

func TestStoreEnginesSeparate(t *testing.T) {
	s := NewStore()
	seen(s, func(seq uint64, sec float64) Hit { return hit(seq, 0, sec) })
	seen(s, func(seq uint64, sec float64) Hit { h := hit(seq, 0, sec); h.Engine = "det2"; return h })
	if got := s.Snapshot(at(3)); len(got) != 2 {
		t.Fatalf("two engines gave %d pins, want 2", len(got))
	}
}

func TestStoreVehiclesMerge(t *testing.T) {
	s := NewStore()
	s.Add(hit(1, 0, 0))
	h := hit(1, 0, 1)
	h.Vehicle, h.Producer = "bravo", "bravo.rgb"
	s.Add(h)
	s.Add(hit(2, 0, 2))
	got := s.Snapshot(at(2))
	if len(got) != 1 || !reflect.DeepEqual(got[0].Vehicles, []string{"alpha", "bravo"}) {
		t.Fatalf("got %+v, want 1 pin by alpha and bravo", got)
	}
}

func TestStorePositionIsMeanOfBoxes(t *testing.T) {
	s := NewStore()
	s.Add(hit(1, 0, 0))
	s.Add(hit(2, 4, 1))
	s.Add(hit(3, 2, 2))
	got := s.Snapshot(at(2))
	if len(got) != 1 {
		t.Fatalf("got %d pins, want 1", len(got))
	}
	_, e := metres(testLat, testLon, got[0].Lat, got[0].Long)
	if math.Abs(e-2) > 0.05 {
		t.Fatalf("pin %.3f m east, want 2", e)
	}
}

func TestStoreExpiry(t *testing.T) {
	s := NewStore()
	seen(s, func(seq uint64, sec float64) Hit { return hit(seq, 0, sec) })
	// last seen at 2 s; expiry 60 s
	if got := s.Snapshot(at(61)); len(got) != 1 {
		t.Fatalf("59 s after last sighting got %d pins, want 1", len(got))
	}
	if got := s.Snapshot(at(63)); len(got) != 0 {
		t.Fatalf("61 s after last sighting got %d pins, want 0", len(got))
	}
}

func TestStoreExpiryChangeAppliesToExisting(t *testing.T) {
	s := NewStore()
	if err := s.SetSettings(Settings{RadiusM: 5, ExpiryS: 120, MinSightings: 3}); err != nil {
		t.Fatal(err)
	}
	seen(s, func(seq uint64, sec float64) Hit { return hit(seq, 0, sec) })
	if got := s.Snapshot(at(92)); len(got) != 1 {
		t.Fatalf("90 s after at 120 s expiry got %d pins, want 1", len(got))
	}
	if err := s.SetSettings(Settings{RadiusM: 5, ExpiryS: 60, MinSightings: 3}); err != nil {
		t.Fatal(err)
	}
	if got := s.Snapshot(at(92)); len(got) != 0 {
		t.Fatalf("90 s after at 60 s expiry got %d pins, want 0", len(got))
	}
}

func TestStoreRadiusChangeDoesNotRegroup(t *testing.T) {
	s := NewStore()
	seen(s, func(seq uint64, sec float64) Hit { return hit(seq, 0, sec) })
	seen(s, func(seq uint64, sec float64) Hit { return hit(seq+10, 8, sec) })
	if err := s.SetSettings(Settings{RadiusM: 10, ExpiryS: 60, MinSightings: 3}); err != nil {
		t.Fatal(err)
	}
	if got := s.Snapshot(at(3)); len(got) != 2 {
		t.Fatalf("after radius change got %d pins, want 2", len(got))
	}
}

func TestStoreSnapshotTimes(t *testing.T) {
	s := NewStore()
	seen(s, func(seq uint64, sec float64) Hit { return hit(seq, 0, sec) })
	got := s.Snapshot(at(12))
	if len(got) != 1 || got[0].FirstSeenS != 12 || got[0].LastSeenS != 10 {
		t.Fatalf("got %+v, want first seen 12 s ago, last 10 s ago", got)
	}
}

func TestStoreClear(t *testing.T) {
	s := NewStore()
	seen(s, func(seq uint64, sec float64) Hit { return hit(seq, 0, sec) })
	seen(s, func(seq uint64, sec float64) Hit { return hit(seq+10, 50, sec) })
	s.Add(hit(30, 100, 2))
	if n := s.Clear(); n != 2 {
		t.Fatalf("Clear() = %d, want 2", n)
	}
	if got := s.Snapshot(at(3)); len(got) != 0 {
		t.Fatalf("after Clear got %d pins", len(got))
	}
	s.Add(hit(40, 0, 3))
	s.Add(hit(41, 0, 3.5))
	if got := s.Snapshot(at(4)); len(got) != 0 {
		t.Fatalf("2 frames after Clear gave %d pins, want 0 (fresh candidate)", len(got))
	}
}

func TestStoreCap(t *testing.T) {
	if MaxGroups != 500 {
		t.Fatalf("MaxGroups = %d, want 500", MaxGroups)
	}
	s := NewStore()
	seen(s, func(seq uint64, sec float64) Hit { return hit(seq, 0, sec) })
	for i := range 2500 {
		s.Add(hit(uint64(100+i), float64(20*(i+1)), 3))
	}
	if n := s.groupCount(); n > MaxGroups {
		t.Fatalf("%d groups, want at most %d", n, MaxGroups)
	}
	if got := s.Snapshot(at(3)); len(got) != 1 {
		t.Fatalf("visible pin evicted: got %d pins, want 1", len(got))
	}
}

func TestSettingsValidate(t *testing.T) {
	bad := []Settings{
		{RadiusM: 0, ExpiryS: 60, MinSightings: 3},
		{RadiusM: 21, ExpiryS: 60, MinSightings: 3},
		{RadiusM: 5, ExpiryS: 59, MinSightings: 3},
		{RadiusM: 5, ExpiryS: 3601, MinSightings: 3},
		{RadiusM: 5, ExpiryS: 90, MinSightings: 3},
		{RadiusM: 5, ExpiryS: 60, MinSightings: 0},
		{RadiusM: 5, ExpiryS: 60, MinSightings: 6},
	}
	for _, v := range bad {
		if v.Validate() == nil {
			t.Errorf("%+v valid, want error", v)
		}
	}
	if err := DefaultSettings.Validate(); err != nil {
		t.Errorf("defaults invalid: %v", err)
	}
	if DefaultSettings != (Settings{RadiusM: 5, ExpiryS: 60, MinSightings: 3}) {
		t.Errorf("defaults %+v", DefaultSettings)
	}
}

func TestStoreNewObjectAtCapBecomesPin(t *testing.T) {
	s := NewStore()
	for i := range MaxGroups {
		s.Add(hit(uint64(100+i), float64(20*(i+1)), 0))
	}
	far := func(seq uint64, sec float64) Hit {
		h := hit(seq, 0, sec)
		h.Class = "person"
		return h
	}
	s.Add(far(1, 0.1))
	s.Add(far(2, 0.2))
	s.Add(far(3, 0.3))
	got := s.Snapshot(at(0.3))
	if len(got) != 1 || got[0].Class != "person" {
		t.Fatalf("got %+v, want the new person pin despite the cap", got)
	}
}
