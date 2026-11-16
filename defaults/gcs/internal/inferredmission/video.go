package inferredmission

import "sync"

type videoWorker struct {
	end   float64
	ready chan struct{} // closed once vision is persisted or the attempt exits
}

type videoResult struct {
	index int
	err   error
}

func (s *Service) runVideo(r *run, meta session) (string, string) {
	if meta.Video == nil || !validID.MatchString(meta.Video.ID) {
		return "error", "The analysis record is missing its source video."
	}
	path := s.videoPath(meta.Video.ID)
	sum, err := digest(path)
	if err != nil || sum != meta.Video.SHA256 {
		return "error", "The uploaded video has changed or is missing."
	}

	cfg := meta.Settings
	sliding := cfg.SectionMode == sectionSliding
	limit := 1
	if sliding {
		limit = slidingVideoLimit
	}
	workers := make(map[int]videoWorker, limit)
	done := make(chan videoResult, limit)
	var workErr error
	reap := func(result videoResult) {
		delete(workers, result.index)
		if workErr == nil {
			workErr = result.err
		}
	}

schedule:
	for index := 1; index <= meta.Total; index++ {
		if len(workers) == limit {
			reap(<-done)
		}
		if s.ctx.Err() != nil || workErr != nil {
			break
		}
		r.mu.Lock()
		status, previous := r.meta.Status, r.rows[index]
		r.mu.Unlock()
		if status != "analyzing" {
			break
		}
		if finished(previous.Status) {
			continue
		}
		if sliding {
			for {
				r.mu.Lock()
				status, changed := r.meta.Status, r.changed
				r.mu.Unlock()
				if status != "analyzing" || workErr != nil {
					break schedule
				}
				select {
				case s.videoSlots <- struct{}{}:
				case result := <-done:
					reap(result)
					continue
				case <-changed:
					continue
				case <-s.ctx.Done():
					break schedule
				}
				break
			}
		}
		// Linearize admission against Pause/Stop after acquiring a slot.
		r.mu.Lock()
		admitted := r.meta.Status == "analyzing" && s.ctx.Err() == nil
		r.mu.Unlock()
		if !admitted {
			if sliding {
				<-s.videoSlots
			}
			break
		}

		start, end := cfg.window(index, meta.Video.Duration)
		row := section{sectionSummary: sectionSummary{Index: index, Start: start, End: end}, InputQuality: "complete"}
		if end < start+cfg.SectionSeconds {
			row.InputQuality = "partial"
		}
		var historyReady []<-chan struct{}
		if cfg.HistoryMode != "no_memory" && cfg.HistorySections > 0 {
			for _, worker := range workers {
				if worker.end <= start {
					historyReady = append(historyReady, worker.ready)
				}
			}
		}
		ready := make(chan struct{})
		workers[index] = videoWorker{end, ready}
		// The scheduler acquires slots in section order. A worker never
		// waits for a later window, even across different runs.
		go func() {
			publishVision := sync.OnceFunc(func() { close(ready) })
			load := func() ([]sample, error) {
				return sampleVideo(s.ctx, path, framesDir(r, index), start, end, cfg)
			}
			err := s.processSection(r, meta, row, load, historyReady, publishVision)
			publishVision()
			if sliding {
				<-s.videoSlots
			}
			done <- videoResult{index, err}
		}()
	}
	for len(workers) != 0 {
		reap(<-done)
	}
	if s.ctx.Err() != nil {
		return "paused", "GCS stopped during analysis."
	}
	if workErr != nil {
		s.log.Warn().Err(workErr).Msg("could not save video analysis")
		return "paused", "Could not save the analysis section."
	}
	return "completed", ""
}
