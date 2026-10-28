package main

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"time"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
)

// NAL unit types the access unit split cares about.
const (
	nalSlice = 1
	nalIDR   = 5
	nalAUD   = 9
)

// AccessUnit is one H.264 access unit as an Annex-B byte stream.
type AccessUnit struct {
	Data []byte
	// Key is whether the unit has an IDR slice.
	Key bool
}

// SplitAccessUnits splits an Annex-B stream into its access units. The NAL
// units before a frame's first slice (SPS, PPS, SEI, AUD) are part of that
// frame's unit, so a keyframe carries the parameter sets it needs. The units'
// bytes, concatenated, are the stream from its first start code.
func SplitAccessUnits(annexB []byte) ([]AccessUnit, error) {
	starts := nalStarts(annexB)
	if len(starts) == 0 {
		return nil, errors.New("no start code in the stream")
	}
	var units []AccessUnit
	cur := AccessUnit{}
	unitStart := starts[0].at
	hasSlice := false // whether cur has a slice yet
	prefixAt := -1    // where the non-slice NALs after cur's last slice start
	for _, s := range starts {
		if s.header >= len(annexB) {
			continue
		}
		switch typ := annexB[s.header] & 0x1f; {
		case typ >= nalSlice && typ <= nalIDR:
			// A slice with first_mb_in_slice = 0 (a 1 bit in ue(v)) starts a
			// new picture, as does any slice after prefix NALs
			firstMB := s.header+1 < len(annexB) && annexB[s.header+1]&0x80 != 0
			if hasSlice && (firstMB || prefixAt >= 0) {
				end := s.at
				if prefixAt >= 0 {
					end = prefixAt
				}
				cur.Data = annexB[unitStart:end]
				units = append(units, cur)
				cur, unitStart = AccessUnit{}, end
			}
			hasSlice, prefixAt = true, -1
			if typ == nalIDR {
				cur.Key = true
			}
		case typ == nalAUD || (typ >= 6 && typ <= 8) || (typ >= 13 && typ <= 18):
			// SEI, SPS, PPS, AUD and the other NALs that may only precede a
			// picture's first slice
			if hasSlice && prefixAt < 0 {
				prefixAt = s.at
			}
		}
	}
	if !hasSlice {
		return nil, errors.New("no slice in the stream")
	}
	cur.Data = annexB[unitStart:]
	return append(units, cur), nil
}

// nalStart is where a NAL unit's start code begins, and where its header
// byte is.
type nalStart struct{ at, header int }

// nalStarts finds the NAL units of an Annex-B stream. A zero byte before a
// 3-byte start code is part of it (a 4-byte start code).
func nalStarts(b []byte) []nalStart {
	var out []nalStart
	for i := 0; i+3 <= len(b); {
		if b[i] != 0 || b[i+1] != 0 || b[i+2] != 1 {
			i++
			continue
		}
		at := i
		if at > 0 && b[at-1] == 0 {
			at--
		}
		out = append(out, nalStart{at: at, header: i + 3})
		i += 3
	}
	return out
}

// Clip parameters: the stream is constrained baseline with no B-frames, as
// the GCS's WebRTC track expects, with a keyframe every second.
const (
	clipSize     = "640x360"
	clipFPS      = 15
	clipSeconds  = 10
	clipKeyEvery = 15
)

// unsafeText matches what can't be drawn in a clip's label as is.
var unsafeText = regexp.MustCompile(`[^A-Za-z0-9_-]`)

// EnsureClip returns the path of vehicle's clip in dir, generating it with
// ffmpeg first unless it already exists.
func EnsureClip(dir, vehicle string) (string, error) {
	path := filepath.Join(dir, vehicle+".h264")
	if info, err := os.Stat(path); err == nil && info.Size() > 0 {
		return path, nil
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	tmp, err := os.CreateTemp(dir, vehicle+".*.h264.tmp")
	if err != nil {
		return "", err
	}
	tmp.Close()
	defer os.Remove(tmp.Name())

	_, short := fleet.SplitName(vehicle)
	cmd := exec.Command("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
		"-f", "lavfi", "-i", fmt.Sprintf("testsrc2=size=%s:rate=%d:duration=%d", clipSize, clipFPS, clipSeconds),
		"-vf", labelFilter(unsafeText.ReplaceAllString(short, "_")),
		"-an", "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.1", "-pix_fmt", "yuv420p",
		"-bf", "0", "-g", fmt.Sprint(clipKeyEvery), "-keyint_min", fmt.Sprint(clipKeyEvery), "-sc_threshold", "0",
		// Every keyframe carries the parameter sets, so a viewer can start there
		"-x264-params", "repeat-headers=1",
		"-f", "h264", tmp.Name())
	if out, err := cmd.CombinedOutput(); err != nil {
		return "", fmt.Errorf("could not generate the clip of %s: %w: %s", vehicle, err, bytes.TrimSpace(out))
	}
	if err := os.Rename(tmp.Name(), path); err != nil {
		return "", err
	}
	return path, nil
}

// labelFilter draws text in the middle of the frame. ffmpeg's fontconfig
// finds no font in the Pixi environment, so the filter names the
// environment's own font; without one, the clip has no label.
func labelFilter(text string) string {
	font := filepath.Join(os.Getenv("CONDA_PREFIX"), "fonts", "DejaVuSans.ttf")
	if _, err := os.Stat(font); os.Getenv("CONDA_PREFIX") == "" || err != nil {
		return "null"
	}
	return fmt.Sprintf("drawtext=fontfile='%s':text='%s':fontsize=72:fontcolor=white:"+
		"box=1:boxcolor=black@0.6:boxborderw=12:x=(w-tw)/2:y=(h-th)/2", font, text)
}

// resumeGap is how long a vehicle's frames may stop for before its clip
// resumes at its next keyframe: a viewer that lost the frames in between
// can't decode the P-frames that would follow.
const resumeGap = time.Second

// clipCursor keeps each vehicle's place in its clip. It isn't safe for
// concurrent use.
type clipCursor struct {
	clips map[string][]AccessUnit
	next  map[string]int       // the index of the vehicle's next unit
	last  map[string]time.Time // when the vehicle's last unit was taken
}

func newClipCursor(clips map[string][]AccessUnit) *clipCursor {
	return &clipCursor{clips: clips, next: map[string]int{}, last: map[string]time.Time{}}
}

// Next returns the vehicle's next access unit at time now, looping its clip.
// After more than resumeGap without one, it skips to the clip's next
// keyframe. It returns false if the vehicle has no clip.
func (c *clipCursor) Next(vehicle string, now time.Time) (AccessUnit, bool) {
	units := c.clips[vehicle]
	if len(units) == 0 {
		return AccessUnit{}, false
	}
	i := c.next[vehicle] % len(units)
	if last, ok := c.last[vehicle]; ok && now.Sub(last) > resumeGap {
		for j := range len(units) {
			if k := (i + j) % len(units); units[k].Key {
				i = k
				break
			}
		}
	}
	c.next[vehicle], c.last[vehicle] = i+1, now
	return units[i], true
}
