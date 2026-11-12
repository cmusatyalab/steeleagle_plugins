package inferredmission

import (
	"errors"
	"math"
)

type Settings struct {
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
		SectionSeconds: 15, FrameFPS: 1, MaxFrameSide: 768, HistoryMode: "recency", HistorySections: 10,
		HalfLifeSections: 3, RequestTimeout: 60, GenerationTimeout: 120,
		MaxVideoBytes: 1 << 30, MaxVideoSeconds: 3600, MaxActiveRuns: 4,
	}
}

func (s Settings) validate() error {
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
	switch s.HistoryMode {
	case "recency", "uniform", "no_memory":
		return nil
	default:
		return errors.New("Unknown history mode.")
	}
}
