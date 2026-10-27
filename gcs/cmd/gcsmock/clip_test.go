package main

import (
	"bytes"
	"os"
	"os/exec"
	"slices"
	"testing"
	"time"
)

// nal returns a NAL unit with a 4-byte start code.
func nal(b ...byte) []byte {
	return append([]byte{0, 0, 0, 1}, b...)
}

func TestSplitAccessUnits(t *testing.T) {
	// Slices start with first_mb_in_slice = 0, whose ue(v) code is a 1 bit,
	// so the byte after the NAL header has its high bit set
	sps := nal(0x67, 0x42, 0xc0, 0x1f)
	pps := nal(0x68, 0xcb, 0x83)
	idr := nal(0x65, 0x88, 0x84, 0x00)
	p1 := nal(0x41, 0x9a, 0x01)
	p2 := nal(0x41, 0x9a, 0x02)
	want := [][]byte{
		bytes.Join([][]byte{sps, pps, idr}, nil),
		p1,
		p2,
		bytes.Join([][]byte{sps, pps, idr}, nil),
	}
	stream := bytes.Join(want, nil)

	units, err := SplitAccessUnits(stream)
	if err != nil {
		t.Fatal(err)
	}
	if len(units) != len(want) {
		t.Fatalf("got %d units, want %d", len(units), len(want))
	}
	keys := []bool{true, false, false, true}
	for i, u := range units {
		if u.Key != keys[i] {
			t.Errorf("unit %d: Key = %v, want %v", i, u.Key, keys[i])
		}
		if !bytes.Equal(u.Data, want[i]) {
			t.Errorf("unit %d: data = %x, want %x", i, u.Data, want[i])
		}
	}
}

func TestSplitAccessUnitsRejectsNoStartCode(t *testing.T) {
	if _, err := SplitAccessUnits([]byte{0x65, 0x88}); err == nil {
		t.Fatal("expected an error for a stream without a start code")
	}
}

func TestEnsureClip(t *testing.T) {
	if _, err := exec.LookPath("ffmpeg"); err != nil {
		t.Skip("ffmpeg is not on PATH; run in the Pixi environment")
	}
	dir := t.TempDir()
	path, err := EnsureClip(dir, "100.64.0.1.alpha")
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	units, err := SplitAccessUnits(data)
	if err != nil {
		t.Fatal(err)
	}
	if len(units) == 0 || !units[0].Key {
		t.Fatalf("the first of %d units isn't a keyframe", len(units))
	}
	keys := 0
	for _, u := range units {
		if u.Key {
			keys++
		}
	}
	// 10 s at 15 fps with a keyframe every 15 frames
	if len(units) != 150 || keys != 10 {
		t.Errorf("got %d units with %d keyframes, want 150 with 10", len(units), keys)
	}

	before, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	again, err := EnsureClip(dir, "100.64.0.1.alpha")
	if err != nil {
		t.Fatal(err)
	}
	if again != path {
		t.Errorf("second call returned %q, want %q", again, path)
	}
	after, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if !after.ModTime().Equal(before.ModTime()) {
		t.Error("the cached clip was regenerated")
	}
}

func TestClipCursor(t *testing.T) {
	// Keyframes at 0 and 3
	units := []AccessUnit{{Key: true}, {}, {}, {Key: true}, {}}
	for i := range units {
		units[i].Data = []byte{byte(i)}
	}
	c := newClipCursor(map[string][]AccessUnit{"a": units, "b": units})
	at := time.Unix(1000, 0)
	tick := func(vehicle string, d time.Duration) int {
		t.Helper()
		at = at.Add(d)
		u, ok := c.Next(vehicle, at)
		if !ok {
			t.Fatalf("no unit for %s", vehicle)
		}
		return int(u.Data[0])
	}
	frame := time.Second / clipFPS
	var got []int
	for range 7 {
		got = append(got, tick("a", frame))
	}
	// Loops the clip
	if want := []int{0, 1, 2, 3, 4, 0, 1}; !slices.Equal(got, want) {
		t.Fatalf("units = %v, want %v", got, want)
	}
	// After a gap, resumes at the next keyframe
	if u := tick("a", resumeGap+frame); u != 3 {
		t.Errorf("after a gap got unit %d, want keyframe 3", u)
	}
	if u := tick("a", frame); u != 4 {
		t.Errorf("got unit %d, want 4", u)
	}
	// ... looping to find it
	if u := tick("a", resumeGap+frame); u != 0 {
		t.Errorf("after a gap got unit %d, want keyframe 0", u)
	}
	// A gap that ends on a keyframe stays there
	tick("a", frame)
	tick("a", frame)
	if u := tick("a", resumeGap+frame); u != 3 {
		t.Errorf("after a gap got unit %d, want keyframe 3", u)
	}
	// Each vehicle has its own place and its own gap
	if u := tick("b", frame); u != 0 {
		t.Errorf("b got unit %d, want 0", u)
	}
	if _, ok := c.Next("c", at); ok {
		t.Error("got a unit for a vehicle with no clip")
	}
}
