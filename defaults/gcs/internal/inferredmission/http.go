package inferredmission

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
	"github.com/google/uuid"
)

const api = "/api/inferred-mission"

type Source struct {
	Producer string `json:"producer"`
	Vehicle  string `json:"vehicle"`
}

func (s *Service) Register(mux *http.ServeMux, sources func() []Source) {
	mux.HandleFunc("GET "+api+"/status", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		ctx, cancel := context.WithTimeout(r.Context(), 500*time.Millisecond)
		defer cancel()
		_, err := s.generator.Compiler.DraftReference(ctx, mission.PlanningContext{})
		reply(w, http.StatusOK, map[string]any{
			"settings": s.settings, "models": s.models(), "configured": s.configured(), "media_available": mediaAvailable(),
			"compiler_available": err == nil, "prompt_version": promptVersion,
		})
	})
	mux.HandleFunc("POST "+api+"/videos", s.upload)
	mux.HandleFunc("GET "+api+"/videos/{asset}", s.videoMetadata)
	mux.HandleFunc("GET "+api+"/videos/{asset}/media", s.videoMedia)
	mux.HandleFunc("GET "+api+"/sessions", s.sessions)
	mux.HandleFunc("POST "+api+"/sessions", func(w http.ResponseWriter, r *http.Request) {
		if !s.configured() || !mediaAvailable() {
			failHTTP(w, http.StatusServiceUnavailable, "Configure the vision model and install FFmpeg and ffprobe.")
			return
		}
		var req startRequest
		dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 2<<20))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&req); err != nil {
			failHTTP(w, http.StatusBadRequest, "Invalid analysis session request.")
			return
		}
		if req.Source == "live" {
			found := slices.ContainsFunc(sources(), func(source Source) bool {
				return source.Vehicle == req.Vehicle && source.Producer == req.Producer
			})
			if !found {
				failHTTP(w, http.StatusNotFound, "The selected vehicle camera is not streaming.")
				return
			}
		}
		run, err := s.start(req)
		if err != nil {
			code := http.StatusConflict
			if errors.Is(err, os.ErrNotExist) {
				code = http.StatusNotFound
			}
			failHTTP(w, code, err.Error())
			return
		}
		state, _ := run.snap()
		reply(w, http.StatusCreated, state)
	})
	mux.HandleFunc("GET "+api+"/sessions/{session}", s.withRun(func(w http.ResponseWriter, _ *http.Request, run *run) {
		state, _ := run.snap()
		reply(w, http.StatusOK, state)
	}))
	mux.HandleFunc("PATCH "+api+"/sessions/{session}", s.withRun(s.manageRun))
	mux.HandleFunc("GET "+api+"/sessions/{session}/events", s.withRun(s.events))
	mux.HandleFunc("GET "+api+"/sessions/{session}/sections", s.withRun(s.sections))
	mux.HandleFunc("GET "+api+"/sessions/{session}/sections/{section}", s.withRun(s.section))
	mux.HandleFunc("GET "+api+"/sessions/{session}/sections/{section}/frames/{frame}", s.withRun(s.frame))
	mux.HandleFunc("GET "+api+"/sessions/{session}/sections/{section}/files/{file}", s.withRun(s.artifact))
	for _, action := range []string{"pause", "resume", "stop"} {
		mux.HandleFunc("POST "+api+"/sessions/{session}/"+action, s.withRun(func(w http.ResponseWriter, r *http.Request, run *run) {
			if err := s.change(run, action); err != nil {
				failHTTP(w, http.StatusConflict, err.Error())
				return
			}
			state, _ := run.snap()
			reply(w, http.StatusOK, state)
		}))
	}
}

func (s *Service) change(r *run, action string) error {
	if action == "resume" {
		if !s.configured() || !mediaAvailable() {
			return errors.New("Configure the vision model and media tools before resuming.")
		}
		return s.resume(r)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	status := r.meta.Status
	switch action {
	case "pause":
		if r.meta.Source != "uploaded_video" || status != "analyzing" {
			return errors.New("Only a running video analysis can be paused.")
		}
		return r.setStatus("pausing", "")
	case "stop":
		switch status {
		case "paused":
			return r.setStatus("stopped", "")
		case "observing", "analyzing", "pausing":
			if err := r.setStatus("stopping", ""); err != nil {
				return err
			}
			if r.live != nil {
				r.live.stopCapture()
			}
		}
	}
	return nil
}

func (s *Service) upload(w http.ResponseWriter, r *http.Request) {
	if !mediaAvailable() {
		failHTTP(w, http.StatusServiceUnavailable, "Install FFmpeg and ffprobe to analyze MP4 files.")
		return
	}
	select {
	case s.uploads <- struct{}{}:
		defer func() { <-s.uploads }()
	default:
		failHTTP(w, http.StatusTooManyRequests, "The GCS is handling too many video uploads.")
		return
	}
	limit := s.settings.MaxVideoBytes
	r.Body = http.MaxBytesReader(w, r.Body, limit+(1<<20))
	reader, err := r.MultipartReader()
	if err != nil {
		failHTTP(w, http.StatusBadRequest, "Expected a multipart MP4 upload.")
		return
	}
	part, err := reader.NextPart()
	if err != nil || part.FormName() != "file" || !strings.EqualFold(filepath.Ext(part.FileName()), ".mp4") {
		failHTTP(w, http.StatusBadRequest, "Select an MP4 file.")
		return
	}
	name := part.FileName()
	temp, err := os.CreateTemp(filepath.Join(s.root, "videos"), ".upload-*.mp4")
	if err != nil {
		s.storageError(w, err)
		return
	}
	defer os.Remove(temp.Name())
	sum := sha256.New()
	written, copyErr := io.Copy(io.MultiWriter(temp, sum), io.LimitReader(part, limit+1))
	closeErr := temp.Close()
	var tooLarge *http.MaxBytesError
	switch {
	case written > limit || errors.As(copyErr, &tooLarge):
		failHTTP(w, http.StatusRequestEntityTooLarge, "Video exceeds the maximum upload size.")
		return
	case copyErr != nil || written == 0:
		failHTTP(w, http.StatusBadRequest, "The MP4 upload was empty or interrupted.")
		return
	case closeErr != nil:
		s.storageError(w, closeErr)
		return
	}
	video, err := probeVideo(r.Context(), temp.Name(), s.settings.MaxVideoSeconds)
	if err != nil {
		failHTTP(w, http.StatusUnprocessableEntity, err.Error())
		return
	}
	video.ID, video.Name, video.SHA256 = uuid.NewString(), name, hex.EncodeToString(sum.Sum(nil))
	dir := filepath.Join(s.root, "videos", video.ID)
	if err := os.Mkdir(dir, 0o700); err != nil {
		s.storageError(w, err)
		return
	}
	if err := os.Rename(temp.Name(), s.videoPath(video.ID)); err != nil {
		s.storageError(w, err)
		return
	}
	if err := writeJSONFile(filepath.Join(dir, "asset.json"), video); err != nil {
		s.storageError(w, err)
		return
	}
	reply(w, http.StatusCreated, video)
}

func (s *Service) videoPath(id string) string {
	return filepath.Join(s.root, "videos", id, "source.mp4")
}

func (s *Service) video(id string) (Video, error) {
	var video Video
	if !validID.MatchString(id) || readJSON(filepath.Join(s.root, "videos", id, "asset.json"), &video) != nil || video.ID != id {
		return Video{}, fmt.Errorf("Uploaded video not found: %w", os.ErrNotExist)
	}
	return video, nil
}

func (s *Service) videoMetadata(w http.ResponseWriter, r *http.Request) {
	video, err := s.video(r.PathValue("asset"))
	if err != nil {
		failHTTP(w, http.StatusNotFound, "Uploaded video not found.")
		return
	}
	reply(w, http.StatusOK, video)
}

func (s *Service) videoMedia(w http.ResponseWriter, r *http.Request) {
	video, err := s.video(r.PathValue("asset"))
	if err != nil {
		failHTTP(w, http.StatusNotFound, "Uploaded video not found.")
		return
	}
	w.Header().Set("Content-Type", "video/mp4")
	http.ServeFile(w, r, s.videoPath(video.ID))
}

func (s *Service) sessions(w http.ResponseWriter, r *http.Request) {
	includeArchived, err := strconv.ParseBool(first(r.URL.Query().Get("include_archived"), "false"))
	if err != nil {
		failHTTP(w, http.StatusBadRequest, "Invalid include_archived value.")
		return
	}
	s.mu.RLock()
	runs := make([]*run, 0, len(s.runs))
	for _, run := range s.runs {
		runs = append(runs, run)
	}
	s.mu.RUnlock()
	states := make([]snapshot, 0, len(runs))
	for _, run := range runs {
		run.mu.Lock()
		if includeArchived || run.meta.Archived == nil {
			states = append(states, snapshot{session: run.meta})
		}
		run.mu.Unlock()
	}
	slices.SortFunc(states, func(a, b snapshot) int {
		switch {
		case a.Created < b.Created:
			return 1
		case a.Created > b.Created:
			return -1
		default:
			return strings.Compare(a.ID, b.ID)
		}
	})
	reply(w, http.StatusOK, states)
}

func (s *Service) withRun(fn func(http.ResponseWriter, *http.Request, *run)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		s.mu.RLock()
		run := s.runs[r.PathValue("session")]
		s.mu.RUnlock()
		if run == nil {
			failHTTP(w, http.StatusNotFound, "Analysis session not found.")
			return
		}
		fn(w, r, run)
	}
}

func (s *Service) events(w http.ResponseWriter, r *http.Request, run *run) {
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no")
	flush := http.NewResponseController(w).Flush
	keepalive := time.NewTicker(10 * time.Second)
	defer keepalive.Stop()
	for {
		state, changed := run.snap()
		data, err := json.Marshal(state)
		if err != nil {
			return
		}
		if _, err := fmt.Fprintf(w, "id: %d\nevent: update\ndata: %s\n\n", state.Revision, data); err != nil || flush() != nil {
			return
		}
		if !active(state.Status) {
			return
		}
		select {
		case <-s.ctx.Done():
			return
		case <-r.Context().Done():
			return
		case <-changed:
		case <-keepalive.C:
		}
	}
}

func (s *Service) sections(w http.ResponseWriter, r *http.Request, run *run) {
	state, _ := run.snap()
	offset, err1 := strconv.Atoi(first(r.URL.Query().Get("offset"), "0"))
	limit, err2 := strconv.Atoi(first(r.URL.Query().Get("limit"), "100"))
	if err1 != nil || err2 != nil || offset < 0 || limit < 1 || limit > 250 {
		failHTTP(w, http.StatusBadRequest, "Invalid section pagination.")
		return
	}
	total := len(state.Sections)
	start := min(total, offset)
	reply(w, http.StatusOK, map[string]any{"items": state.Sections[start:min(total, start+limit)], "total": total})
}

func first(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}

func readSection(w http.ResponseWriter, r *http.Request, run *run) (section, bool) {
	index, err := strconv.Atoi(r.PathValue("section"))
	run.mu.Lock()
	row, ok := run.rows[index]
	run.mu.Unlock()
	if err != nil || !ok {
		failHTTP(w, http.StatusNotFound, "Section not found.")
		return section{}, false
	}
	return row, true
}

func (s *Service) section(w http.ResponseWriter, r *http.Request, run *run) {
	if row, ok := readSection(w, r, run); ok {
		reply(w, http.StatusOK, row)
	}
}

func (s *Service) frame(w http.ResponseWriter, r *http.Request, run *run) {
	row, ok := readSection(w, r, run)
	if !ok {
		return
	}
	i, err := strconv.Atoi(r.PathValue("frame"))
	if err != nil || i < 0 || i >= row.FrameCount {
		failHTTP(w, http.StatusNotFound, "Frame not found.")
		return
	}
	w.Header().Set("Content-Type", "image/jpeg")
	http.ServeFile(w, r, frameFile(framesDir(run, row.Index), i))
}

func (s *Service) artifact(w http.ResponseWriter, r *http.Request, run *run) {
	row, ok := readSection(w, r, run)
	if !ok {
		return
	}
	name := r.PathValue("file")
	switch name {
	case "mission.dsl":
		if row.DSL == "" {
			break
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.Header().Set("Content-Disposition", fmt.Sprintf("attachment; filename=\"section-%06d-%s\"", row.Index, name))
		_, _ = io.WriteString(w, row.DSL)
		return
	case "mission.json", "inference.json":
		var data any = row
		if name == "mission.json" {
			if row.Mission == nil {
				break
			}
			data = row.Mission
		}
		run.mu.Lock()
		meta := run.meta
		run.mu.Unlock()
		w.Header().Set("Content-Disposition", jsonDownloadDisposition(meta, row.Index, name))
		reply(w, http.StatusOK, data)
		return
	}
	failHTTP(w, http.StatusNotFound, "This section has no such artifact.")
}

func (s *Service) storageError(w http.ResponseWriter, err error) {
	s.log.Warn().Err(err).Msg("could not save video asset")
	failHTTP(w, http.StatusInternalServerError, "Could not save the video. Check GCS storage.")
}

func failHTTP(w http.ResponseWriter, status int, detail string) {
	reply(w, status, map[string]string{"detail": detail})
}

func reply(w http.ResponseWriter, status int, data any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(data)
}
