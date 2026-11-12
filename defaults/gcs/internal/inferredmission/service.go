// Package inferredmission analyzes uploaded MP4s and live camera streams.
package inferredmission

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"maps"
	"math"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/nl2dsl"
	"github.com/google/uuid"
	"github.com/rs/zerolog"
)

type sectionSummary struct {
	Index           int     `json:"index"`
	Start           float64 `json:"start"`
	End             float64 `json:"end"`
	Status          string  `json:"status"`
	InferenceStatus string  `json:"inference_status,omitempty"`
	CompileStatus   string  `json:"compile_status,omitempty"`
	InferenceText   string  `json:"inference_text,omitempty"`
	Error           string  `json:"error,omitempty"`
}

type section struct {
	sectionSummary
	InputQuality   string           `json:"input_quality,omitempty"`
	FrameCount     int              `json:"frame_count"`
	FrameTimes     []float64        `json:"frame_times,omitempty"`
	CaptureTimes   []*float64       `json:"capture_times,omitempty"`
	FrameIDs       []*uint64        `json:"frame_ids,omitempty"`
	UsedReceipt    bool             `json:"uses_gcs_receive_time,omitempty"`
	Telemetry      map[string]any   `json:"telemetry,omitempty"`
	History        []historyItem    `json:"history,omitempty"`
	VisionPrompt   string           `json:"vision_prompt,omitempty"`
	DSLReference   map[string]any   `json:"dsl_reference,omitempty"`
	Task           *task            `json:"task,omitempty"`
	Instruction    string           `json:"instruction,omitempty"`
	Adaptation     string           `json:"adaptation,omitempty"`
	Unresolved     []string         `json:"unresolved,omitempty"`
	Attempts       []mission.Draft  `json:"attempts,omitempty"`
	DSL            string           `json:"dsl,omitempty"`
	Mission        *missionArtifact `json:"mission_json,omitempty"`
	Elapsed        float64          `json:"elapsed,omitempty"`
	InferenceTime  float64          `json:"inference_elapsed,omitempty"`
	GenerationTime float64          `json:"generation_elapsed,omitempty"`
}

type missionArtifact struct {
	SchemaVersion int           `json:"schema_version"`
	Graph         mission.Graph `json:"graph"`
}

type startRequest struct {
	Mode     string                  `json:"mode,omitempty"`
	Source   string                  `json:"source_type"`
	Asset    string                  `json:"video_asset_id,omitempty"`
	Vehicle  string                  `json:"vehicle_id,omitempty"`
	Producer string                  `json:"producer,omitempty"`
	History  string                  `json:"history_mode,omitempty"`
	Flight   *float64                `json:"flight_started_at,omitempty"`
	Offset   *float64                `json:"video_takeoff_offset,omitempty"`
	Plan     mission.PlanningContext `json:"planning_context"`
}

type session struct {
	ID            string                  `json:"id"`
	SchemaVersion int                     `json:"schema_version"`
	Mode          string                  `json:"mode"`
	Created       float64                 `json:"created_at"`
	Revision      uint64                  `json:"revision"`
	Status        string                  `json:"status"`
	Source        string                  `json:"source_type"`
	Settings      Settings                `json:"settings"`
	Models        map[string]string       `json:"models"`
	PromptVersion string                  `json:"prompt_version"`
	PromptDigest  string                  `json:"prompt_sha256"`
	Video         *Video                  `json:"video,omitempty"`
	Vehicle       string                  `json:"vehicle_id,omitempty"`
	Producer      string                  `json:"producer,omitempty"`
	Total         int                     `json:"total_sections,omitempty"`
	Flight        *float64                `json:"flight_started_at,omitempty"`
	Offset        *float64                `json:"video_takeoff_offset,omitempty"`
	Plan          mission.PlanningContext `json:"planning_context"`
	Error         string                  `json:"error,omitempty"`
}

type snapshot struct {
	session
	Sections   []sectionSummary `json:"sections"`
	LastFrame  float64          `json:"last_frame_received,omitempty"`
	Collecting int              `json:"collecting_section,omitempty"`
	FrameCount int              `json:"collecting_frames"`
	Notice     string           `json:"source_notice,omitempty"`
}

type run struct {
	mu         sync.Mutex
	meta       session
	dir        string
	prompt     string
	rows       map[int]section
	changed    chan struct{}
	running    bool
	lastFrame  float64
	collecting int
	frameCount int
	notice     string
	live       *liveInput
}

func (r *run) sectionDir(index int) string {
	return filepath.Join(r.dir, "sections", fmt.Sprintf("%06d", index))
}

// The caller holds r.mu.
func (r *run) update() {
	r.meta.Revision++
	close(r.changed)
	r.changed = make(chan struct{})
}

func (r *run) snap() (snapshot, <-chan struct{}) {
	r.mu.Lock()
	defer r.mu.Unlock()
	rows := make([]sectionSummary, 0, len(r.rows))
	for _, row := range r.rows {
		rows = append(rows, row.sectionSummary)
	}
	slices.SortFunc(rows, func(a, b sectionSummary) int { return a.Index - b.Index })
	return snapshot{session: r.meta, Sections: rows, LastFrame: r.lastFrame, Collecting: r.collecting,
		FrameCount: r.frameCount, Notice: r.notice}, r.changed
}

func (r *run) setSection(row section) error {
	dir := r.sectionDir(row.Index)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	if err := writeJSONFile(filepath.Join(dir, "record.json"), row); err != nil {
		return err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.rows[row.Index] = row
	r.update()
	return nil
}

func (r *run) setStatus(status, detail string) error {
	previous := r.meta
	r.meta.Status, r.meta.Error = status, detail
	r.meta.Revision++
	if err := writeJSONFile(filepath.Join(r.dir, "session.json"), r.meta); err != nil {
		r.meta = previous
		return err
	}
	close(r.changed)
	r.changed = make(chan struct{})
	return nil
}

type Service struct {
	root       string
	settings   Settings
	generator  nl2dsl.Generator
	vision     nl2dsl.Provider
	generation nl2dsl.Provider
	log        zerolog.Logger
	ctx        context.Context
	cancel     context.CancelFunc

	mu      sync.RWMutex
	runs    map[string]*run
	live    map[string]*liveInput
	wg      sync.WaitGroup
	closed  bool
	uploads chan struct{}
}

func New(ctx context.Context, root string, settings Settings, vision, generation nl2dsl.Provider, generator nl2dsl.Generator, log zerolog.Logger) (*Service, error) {
	if err := settings.validate(); err != nil {
		return nil, err
	}
	for _, dir := range []string{"runs", "videos"} {
		if err := os.MkdirAll(filepath.Join(root, dir), 0o700); err != nil {
			return nil, err
		}
	}
	ctx, cancel := context.WithCancel(ctx)
	s := &Service{root: root, settings: settings, generator: generator, vision: vision, generation: generation, log: log,
		ctx: ctx, cancel: cancel, runs: map[string]*run{}, live: map[string]*liveInput{}, uploads: make(chan struct{}, 2)}
	s.recover()
	return s, nil
}

func (s *Service) Close() {
	s.mu.Lock()
	s.closed = true
	s.cancel()
	s.mu.Unlock()
	s.wg.Wait()
}

func (s *Service) models() map[string]string {
	// Endpoint hashes distinguish resumed runs without serializing endpoints or credentials.
	return map[string]string{
		"vision_provider": s.vision.Name, "vision_model": s.vision.ModelID, "vision_reasoning_effort": s.vision.Reasoning,
		"dsl_provider": s.generation.Name, "dsl_model": s.generation.ModelID, "reasoning_effort": s.generation.Reasoning,
		"vision_endpoint_hash": hash(s.vision.BaseURL), "dsl_endpoint_hash": hash(s.generation.BaseURL),
	}
}

func hash(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:])
}

func (s *Service) configured() bool { return s.vision.Configured() && s.generation.Configured() }

func (s *Service) recover() {
	dirs, err := os.ReadDir(filepath.Join(s.root, "runs"))
	if err != nil {
		s.log.Warn().Err(err).Msg("could not read saved analysis sessions")
	}
	for _, entry := range dirs {
		id := entry.Name()
		if !entry.IsDir() || !validID.MatchString(id) {
			continue
		}
		dir := filepath.Join(s.root, "runs", id)
		r := &run{dir: dir, rows: map[int]section{}, changed: make(chan struct{})}
		if readJSON(filepath.Join(dir, "session.json"), &r.meta) != nil ||
			r.meta.SchemaVersion != 1 || r.meta.ID != id || r.meta.Settings.validate() != nil {
			continue
		}
		paths, _ := filepath.Glob(filepath.Join(dir, "sections", "*", "record.json"))
		for _, path := range paths {
			var row section
			if readJSON(path, &row) != nil || row.Index <= 0 {
				continue
			}
			if !finished(row.Status) && active(r.meta.Status) {
				row.Status = "interrupted"
				row.Error = "GCS restarted during analysis."
				if err := writeJSONFile(path, row); err != nil {
					s.log.Warn().Err(err).Msg("could not save interrupted section")
				}
			}
			r.rows[row.Index] = row
		}
		if active(r.meta.Status) {
			status := "paused"
			if r.meta.Source == "live" {
				status = "stopped"
			}
			if err := r.setStatus(status, "GCS restarted during analysis."); err != nil {
				s.log.Warn().Err(err).Msg("could not recover analysis status")
				r.meta.Status = status
			}
		}
		s.runs[id] = r
	}
}

func (s *Service) start(req startRequest) (*run, error) {
	if req.Mode != "" && req.Mode != "analysis_only" {
		return nil, errors.New("Only analysis-only sessions are supported.")
	}
	cfg := s.settings
	if req.History != "" {
		cfg.HistoryMode = req.History
	}
	if err := cfg.validate(); err != nil {
		return nil, err
	}
	if req.Flight != nil && (*req.Flight <= 0 || *req.Flight > seconds(time.Now())) {
		return nil, errors.New("The takeoff time must be in the past.")
	}
	r := &run{rows: map[int]section{}, changed: make(chan struct{}), prompt: visionPrompt, running: true}
	r.meta = session{ID: uuid.NewString(), SchemaVersion: 1, Mode: "analysis_only", Created: seconds(time.Now()),
		Source: req.Source, Settings: cfg, Models: s.models(), PromptVersion: promptVersion, PromptDigest: hash(visionPrompt),
		Vehicle: req.Vehicle, Producer: req.Producer, Flight: req.Flight, Offset: req.Offset, Plan: req.Plan}
	switch req.Source {
	case "uploaded_video":
		video, err := s.video(req.Asset)
		if err != nil {
			return nil, err
		}
		if req.Offset != nil && (*req.Offset < 0 || *req.Offset >= video.Duration) {
			return nil, errors.New("The takeoff offset must be within the video.")
		}
		r.meta.Video, r.meta.Status = &video, "analyzing"
		r.meta.Total = int(math.Ceil(video.Duration / cfg.SectionSeconds))
	case "live":
		if req.Vehicle == "" || req.Producer == "" {
			return nil, errors.New("Select a vehicle camera to observe.")
		}
		r.meta.Status = "observing"
		r.notice = "Waiting for fresh camera frames."
		r.live = newLiveInput(req.Vehicle)
	default:
		return nil, errors.New("Choose an MP4 or a live camera.")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.checkCapacity(); err != nil {
		return nil, err
	}
	if r.live != nil && s.live[req.Producer] != nil {
		return nil, errors.New("This camera is already being observed.")
	}
	r.dir = filepath.Join(s.root, "runs", r.meta.ID)
	if err := os.Mkdir(r.dir, 0o700); err != nil {
		return nil, err
	}
	if err := os.WriteFile(filepath.Join(r.dir, "prompt.md"), []byte(r.prompt), 0o600); err != nil {
		return nil, err
	}
	if err := r.setStatus(r.meta.Status, ""); err != nil {
		return nil, err
	}
	s.runs[r.meta.ID] = r
	if r.live != nil {
		s.live[req.Producer] = r.live
	}
	s.launch(r, r.meta)
	return r, nil
}

// checkCapacity requires s.mu. The service never acquires s.mu while holding r.mu.
func (s *Service) checkCapacity() error {
	if s.closed || s.ctx.Err() != nil {
		return errors.New("The GCS is shutting down.")
	}
	count := 0
	for _, other := range s.runs {
		other.mu.Lock()
		if other.running {
			count++
		}
		other.mu.Unlock()
	}
	if count >= s.settings.MaxActiveRuns {
		return errors.New("The active analysis run limit has been reached.")
	}
	return nil
}

func (s *Service) launch(r *run, meta session) {
	s.wg.Go(func() {
		status, detail := "completed", ""
		if meta.Source == "live" {
			status, detail = s.runLive(r, meta)
			s.mu.Lock()
			delete(s.live, meta.Producer)
			s.mu.Unlock()
		} else {
			status, detail = s.runVideo(r, meta)
		}
		r.mu.Lock()
		defer r.mu.Unlock()
		r.running = false
		if r.meta.Status == "pausing" && status == "completed" {
			status = "paused"
		}
		if r.meta.Status == "stopping" && (status == "completed" || status == "paused") {
			status = "stopped"
		}
		r.collecting, r.frameCount = 0, 0
		if err := r.setStatus(status, detail); err != nil {
			s.log.Warn().Err(err).Msg("could not persist final analysis status")
			r.meta.Status, r.meta.Error = "error", "Could not save the analysis session."
			r.update()
		}
	})
}

func (s *Service) resume(r *run) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.checkCapacity(); err != nil {
		return err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.meta.Source != "uploaded_video" || r.running || r.meta.Status != "paused" {
		return errors.New("Only a paused video analysis can be resumed.")
	}
	if !maps.Equal(s.models(), r.meta.Models) {
		return errors.New("Model settings changed. Start a new analysis to use them.")
	}
	prompt, err := os.ReadFile(filepath.Join(r.dir, "prompt.md"))
	if err != nil || hash(string(prompt)) != r.meta.PromptDigest {
		return errors.New("The archived prompt is missing or changed.")
	}
	if err := r.setStatus("analyzing", ""); err != nil {
		return err
	}
	r.prompt, r.running = string(prompt), true
	s.launch(r, r.meta)
	return nil
}

func active(status string) bool {
	return status == "analyzing" || status == "pausing" || status == "observing" || status == "stopping"
}

func finished(status string) bool {
	return status == "completed" || status == "failed" || status == "gap" || status == "skipped"
}

func seconds(t time.Time) float64 { return float64(t.UnixNano()) / float64(time.Second) }

func timeout(seconds float64) time.Duration { return time.Duration(seconds * float64(time.Second)) }

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
	for index := 1; index <= meta.Total; index++ {
		if s.ctx.Err() != nil {
			return "paused", "GCS stopped during analysis."
		}
		r.mu.Lock()
		status, row := r.meta.Status, r.rows[index]
		r.mu.Unlock()
		if status != "analyzing" {
			break
		}
		if finished(row.Status) {
			continue
		}
		start, end := float64(index-1)*cfg.SectionSeconds, math.Min(float64(index)*cfg.SectionSeconds, meta.Video.Duration)
		row = section{sectionSummary: sectionSummary{Index: index, Start: start, End: end}, InputQuality: "complete"}
		if end-start < cfg.SectionSeconds {
			row.InputQuality = "partial"
		}
		load := func() ([]sample, error) { return sampleVideo(s.ctx, path, framesDir(r, index), start, end, cfg) }
		if err := s.process(r, meta, row, load); err != nil {
			s.log.Warn().Err(err).Msg("could not save video analysis")
			return "paused", "Could not save the analysis section."
		}
	}
	return "completed", ""
}

func framesDir(r *run, index int) string { return filepath.Join(r.sectionDir(index), "frames") }

func (s *Service) process(r *run, meta session, row section, load func() ([]sample, error)) error {
	start := time.Now()
	row.Status, row.InferenceStatus, row.CompileStatus = "preparing", "pending", "not_attempted"
	if err := r.setSection(row); err != nil {
		return err
	}
	fail := func(message string) error {
		row.Status, row.Error = "failed", message
		if s.ctx.Err() != nil {
			row.Status, row.Error = "interrupted", "GCS stopped during analysis."
		}
		if row.InferenceStatus == "running" {
			row.InferenceStatus = "failed"
		}
		row.Elapsed = time.Since(start).Seconds()
		return r.setSection(row)
	}
	frames, err := load()
	if err != nil {
		return fail(err.Error())
	}
	images := make([]nl2dsl.Image, len(frames))
	row.FrameCount = len(frames)
	if len(frames) == 1 && row.InputQuality == "complete" && meta.Settings.SectionSeconds*meta.Settings.FrameFPS > 1 {
		row.InputQuality = "sparse"
	}
	for i, frame := range frames {
		images[i] = frame.Image
		row.FrameTimes = append(row.FrameTimes, frame.Time)
		row.CaptureTimes = append(row.CaptureTimes, frame.Capture)
		row.FrameIDs = append(row.FrameIDs, frame.ID)
		row.UsedReceipt = row.UsedReceipt || (meta.Source == "live" && frame.Capture == nil)
	}
	r.mu.Lock()
	previous := slices.SortedFunc(maps.Values(r.rows), func(a, b section) int { return a.Index - b.Index })
	r.mu.Unlock()
	row.History = selectHistory(previous, row.Start, meta.Settings)
	row.VisionPrompt = buildPrompt(r.prompt, row, meta.Settings.HistoryMode)
	row.Status, row.InferenceStatus = "inferring", "running"
	if err := r.setSection(row); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(s.ctx, timeout(meta.Settings.RequestTimeout))
	inferenceStart := time.Now()
	reply, err := s.vision.Complete(ctx, nl2dsl.Request{Instructions: row.VisionPrompt, Images: images})
	cancel()
	row.InferenceTime = time.Since(inferenceStart).Seconds()
	if err != nil {
		return fail(err.Error())
	}
	row.InferenceText = reply.Text
	task, instruction, err := parseInstruction(reply.Text)
	if err != nil {
		row.InferenceStatus = "invalid"
		return fail(err.Error())
	}
	if altitude, ok := row.Telemetry["relative_altitude_m"].(float64); task.Takeoff > 0 && ok && altitude > 2 {
		_, body, _ := strings.Cut(instruction, ", then ")
		if body != "" {
			instruction = strings.ToUpper(body[:1]) + body[1:]
			task.Takeoff = 0
			row.Adaptation = "Omitted takeoff because the sampled telemetry reports relative altitude above 2 m."
		}
	}
	row.Task, row.Instruction = &task, instruction
	row.Status, row.InferenceStatus = "generating", "valid"
	switch task.Kind {
	case "track", "precision_land":
		row.Unresolved = []string{"The inferred class requires an operator to confirm the target and configured detection engine."}
	case "patrol", "survey":
		row.Unresolved = []string{"Confirm the area in the Map tab before deploying."}
	case "climb", "wait":
		row.Unresolved = []string{"Observed waiting time does not establish the remaining task duration."}
	}
	if err := r.setSection(row); err != nil {
		return err
	}
	ctx, cancel = context.WithTimeout(s.ctx, timeout(meta.Settings.GenerationTimeout))
	generationStart := time.Now()
	res, err := s.generator.Generate(ctx, nl2dsl.Options{
		Messages: []nl2dsl.Message{{Role: "user", Content: instruction}}, Plan: meta.Plan,
		Rules: "Generate a draft preserving only the actions, values and transitions of the inferred instruction. " +
			"Use an existing named planning area if confirmed, otherwise mark the area as UNBOUND_AREA. Never invent a camera or detector binding.",
		RequireDSL: true, Check: task.check, RequiredActions: task.actionChain(),
	})
	cancel()
	row.GenerationTime = time.Since(generationStart).Seconds()
	row.DSLReference, row.Attempts = res.Reference, res.Attempts
	if err != nil {
		return fail(err.Error())
	}
	row.CompileStatus = res.Draft.Status
	if res.Draft.Status == "unsupported_task" {
		return fail(strings.Join(res.Draft.Errors, " "))
	}
	if res.Draft.Status != "compiled" && res.Draft.Status != "needs_context" {
		return fail("No DSL candidate passed the V4 compiler and inferred-task checks.")
	}
	row.DSL = res.Draft.DSL
	if res.Draft.Graph != nil {
		row.Mission = &missionArtifact{SchemaVersion: 1, Graph: *res.Draft.Graph}
	}
	if res.Draft.Status == "needs_context" {
		row.Unresolved = append(row.Unresolved, res.Draft.Errors...)
	}
	row.Status, row.Elapsed = "completed", time.Since(start).Seconds()
	return r.setSection(row)
}
