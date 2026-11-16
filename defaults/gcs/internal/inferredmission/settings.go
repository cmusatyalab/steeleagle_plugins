package inferredmission

import (
	"errors"
	"math"
)

const (
	sectionNonOverlapping = "non_overlapping"
	sectionSliding        = "sliding"
	slidingVideoLimit     = 5
)

type Settings struct {
	SectionMode       string  `json:"section_mode"`
	SectionSeconds    float64 `json:"section_seconds"`
	FrameFPS          float64 `json:"frame_fps"`
	MaxFrameSide      int     `json:"max_frame_side"`
	HistoryMode       string  `json:"history_mode"`
	HistorySections   int     `json:"history_sections"`
	HalfLifeSections  float64 `json:"half_life_sections"`
	RequestTimeout    float64 `json:"request_timeout"`
	GenerationTimeout float64 `json:"generation_timeout"`
	MaxVideoBytes     int64   `json:"max_video_bytes"`
	MaxVideoSeconds   float64 `json:"max_video_seconds"`
	MaxActiveRuns     int     `json:"max_active_runs"`
}

func DefaultSettings() Settings {
	return Settings{
		SectionMode: sectionNonOverlapping, SectionSeconds: 15, FrameFPS: 1, MaxFrameSide: 768, HistoryMode: "recency", HistorySections: 10,
		HalfLifeSections: 3, RequestTimeout: 60, GenerationTimeout: 120,
		MaxVideoBytes: 1 << 30, MaxVideoSeconds: 3600, MaxActiveRuns: 4,
	}
}

// Validate checks the supported inference settings and resource limits.
func (s Settings) Validate() error {
	if s.SectionSeconds < 1 || s.SectionSeconds > 60 || s.SectionSeconds*s.FrameFPS < 1 || s.SectionSeconds*s.FrameFPS > 60 ||
		s.MaxFrameSide < 32 || s.MaxFrameSide > 2048 || s.HistorySections < 0 || s.HistorySections > 30 ||
		s.HalfLifeSections <= 0 || s.RequestTimeout <= 0 || s.RequestTimeout > 600 ||
		s.GenerationTimeout <= 0 || s.GenerationTimeout > 600 || s.MaxVideoBytes <= 0 || s.MaxVideoBytes > 1<<34 ||
		s.MaxVideoSeconds < s.SectionSeconds || s.MaxVideoSeconds > 24*3600 || s.MaxActiveRuns < 1 || s.MaxActiveRuns > 8 {
		return errors.New("Invalid inferred mission settings.")
	}
	for _, n := range []float64{s.SectionSeconds, s.FrameFPS, s.HalfLifeSections, s.RequestTimeout, s.GenerationTimeout, s.MaxVideoSeconds} {
		if math.IsNaN(n) || math.IsInf(n, 0) {
			return errors.New("Inferred mission settings must use finite numbers.")
		}
	}
	switch s.SectionMode {
	case "", sectionNonOverlapping, sectionSliding:
	default:
		return errors.New("Unknown section mode.")
	}
	switch s.HistoryMode {
	case "recency", "uniform", "no_memory":
		return nil
	default:
		return errors.New("Unknown history mode.")
	}
}

func (s Settings) withSectionDefault() Settings {
	if s.SectionMode == "" {
		s.SectionMode = sectionNonOverlapping
	}
	return s
}

func (s Settings) stride() float64 {
	if s.SectionMode == sectionSliding {
		return 1
	}
	return s.SectionSeconds
}

func (s Settings) sectionCount(duration float64) int {
	if s.SectionMode == sectionSliding {
		return 1 + int(math.Ceil(math.Max(0, duration-s.SectionSeconds)))
	}
	return int(math.Ceil(duration / s.SectionSeconds))
}

func (s Settings) window(index int, duration float64) (start, end float64) {
	start = float64(index-1) * s.stride()
	return start, math.Min(start+s.SectionSeconds, duration)
}
