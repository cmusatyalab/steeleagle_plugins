package inferredmission

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"time"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/nl2dsl"
)

type Video struct {
	ID       string  `json:"id"`
	Name     string  `json:"name"`
	Duration float64 `json:"duration"`
	Width    int     `json:"width"`
	Height   int     `json:"height"`
	SHA256   string  `json:"sha256"`
}

func mediaAvailable() bool {
	_, ffmpeg := exec.LookPath("ffmpeg")
	_, ffprobe := exec.LookPath("ffprobe")
	return ffmpeg == nil && ffprobe == nil
}

func probeVideo(ctx context.Context, path string, maxSeconds float64) (Video, error) {
	ctx, cancel := context.WithTimeout(ctx, 45*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
		"-format_whitelist", "mov", "-select_streams", "v:0", "-show_entries",
		"stream=width,height,duration:format=duration", "-of", "json", path)
	out, err := cmd.Output()
	if err != nil {
		return Video{}, errors.New("Could not read the MP4 video stream.")
	}
	type stream struct {
		Width, Height int
		Duration      string
	}
	var info struct {
		Streams []stream
		Format  stream
	}
	if json.Unmarshal(out, &info) != nil || len(info.Streams) != 1 {
		return Video{}, errors.New("The MP4 must contain a video stream with a known duration.")
	}
	source := info.Streams[0]
	duration, err := strconv.ParseFloat(source.Duration, 64)
	if err != nil {
		duration, err = strconv.ParseFloat(info.Format.Duration, 64)
	}
	if err != nil || math.IsNaN(duration) || duration <= 0 || duration > maxSeconds {
		return Video{}, fmt.Errorf("Video duration must be within %g seconds.", maxSeconds)
	}
	if source.Width <= 0 || source.Height <= 0 || source.Width > 36_000_000/source.Height {
		return Video{}, errors.New("Video resolution exceeds the analysis limit.")
	}
	if err := exec.CommandContext(ctx, "ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
		"-threads", "1", "-f", "mov", "-i", path, "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-").Run(); err != nil {
		return Video{}, errors.New("The first frame of the MP4 could not be decoded.")
	}
	return Video{Duration: duration, Width: source.Width, Height: source.Height}, nil
}

func digest(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	_, err = io.Copy(h, f)
	return hex.EncodeToString(h.Sum(nil)), err
}

type sample struct {
	nl2dsl.Image
	Capture *float64
	ID      *uint64
}

var samplePTS = regexp.MustCompile(`\bn:\s*\d+\s+pts:\s*(-?\d+)\s+pts_time:`)

func sampleVideo(ctx context.Context, src, dir string, start, end float64, cfg Settings) ([]sample, error) {
	duration := end - start
	seek := start
	selectFrames := fmt.Sprintf("select=isnan(prev_selected_t)+gte(t-prev_selected_t\\,%.9f)", 1/cfg.FrameFPS)
	if cfg.SectionMode == sectionSliding {
		// A section may start in the middle of a global sampling slot. Seek
		// back to the slot boundary to see its first frame, then exclude any
		// selection that precedes this section.
		slot := math.Floor(start * cfg.FrameFPS)
		seek = slot / cfg.FrameFPS
		selectFrames = fmt.Sprintf("select=isnan(prev_selected_t)+gt(floor(t*%.9f+1e-6)\\,floor(prev_selected_t*%.9f+1e-6)),trim=start=%.9f",
			cfg.FrameFPS, cfg.FrameFPS, start-seek)
	}
	// Use the exact timestamp in FFmpeg's microsecond time base rather than
	// the rounded decimal pts_time field, which can round onto a boundary.
	filter := fmt.Sprintf("settb=AVTB,trim=end=%.9f,%s,%s,showinfo", end-seek, selectFrames, scaleFilter(cfg.MaxFrameSide))
	return decode(ctx, dir, nil, []string{"-ss", fmt.Sprintf("%.9f", seek), "-f", "mov", "-i", src}, filter,
		int(math.Ceil((end-seek)*cfg.FrameFPS))+1, func(_ int, pts []byte) (sample, error) {
			microseconds, err := strconv.ParseInt(string(pts), 10, 64)
			at := float64(microseconds-int64(math.Round((start-seek)*1e6))) / 1e6
			if err != nil || at < 0 || at >= duration {
				return sample{}, errors.New("A decoded frame falls outside the section timeline.")
			}
			return sample{Image: nl2dsl.Image{Time: at}}, nil
		})
}

func scaleFilter(side int) string {
	return fmt.Sprintf("scale=%d:%d:force_original_aspect_ratio=decrease", side, side)
}

// decode saves each selected JPEG and matches it with the corresponding
// showinfo timestamp. A mismatch is reported instead of guessing timestamps.
func decode(ctx context.Context, dir string, input io.Reader, inputArgs []string, filter string, max int, metadata func(int, []byte) (sample, error)) ([]sample, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(ctx, 45*time.Second)
	defer cancel()
	args := []string{"-nostdin", "-hide_banner", "-loglevel", "info", "-y", "-protocol_whitelist", "file,pipe", "-threads", "1"}
	args = append(args, inputArgs...)
	args = append(args, "-map", "0:v:0", "-an", "-sn", "-dn", "-map_metadata", "-1", "-filter_threads", "1",
		"-vf", filter, "-fps_mode", "vfr", "-q:v", "3", "-threads", "1", "-frames:v", strconv.Itoa(max),
		"-start_number", "0", filepath.Join(dir, "%04d.jpg"))
	cmd := exec.CommandContext(ctx, "ffmpeg", args...)
	cmd.Stdin = input
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return nil, errors.New("Could not decode video frames for this section.")
	}
	matches := samplePTS.FindAllSubmatch(stderr.Bytes(), -1)
	if len(matches) == 0 {
		return nil, errors.New("No decodable frames in this section.")
	}
	out := make([]sample, 0, len(matches))
	for i, m := range matches {
		if i >= max {
			return nil, errors.New("Decoded frames could not be matched to the section.")
		}
		frame, err := metadata(i, m[1])
		if err != nil {
			return nil, err
		}
		frame.JPEG, err = os.ReadFile(frameFile(dir, i))
		if err != nil {
			return nil, errors.New("Decoded frames could not be matched to the section.")
		}
		out = append(out, frame)
	}
	return out, nil
}

func frameFile(dir string, index int) string {
	return filepath.Join(dir, fmt.Sprintf("%04d.jpg", index))
}
