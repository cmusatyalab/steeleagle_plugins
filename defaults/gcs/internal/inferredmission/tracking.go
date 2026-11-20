package inferredmission

// When a live section's recommendation is Track, the vision model names the
// detection box of the object the pilot is following (target_box). That
// box's detector class becomes the Track class, and the box is checked
// against the cliped verifier's prompt with the verifier's own model: if the
// model says it isn't the prompt, a crop of the box becomes the verifier's
// image prompt (prompt.jpg) through its live directory. The vision model
// also names distractor_boxes, other objects of the target's class in the
// section; their crops are written beside the prompt as d1.jpg, d2.jpg, ...
// (the old ones set aside), so a verifier with image_mode "contrastive"
// describes the target by what sets it apart from them. The verifier turns
// the image into text, and box_rerank follows that text. With auto_deploy, the section's compiled mission is sent to
// the vehicle to compile and load, as its fallback when its data link fails.

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	"image/color"
	"image/jpeg"
	"image/png"
	"io"
	"math"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/nl2dsl"
)

// The questions the cliped verifier asks (cliped/verifier.py), so the GCS
// and the verifier judge a box alike
const (
	verifierBoxQuestion = "This image is cut from an aerial drone video. Is the object inside the red box {prompt}? " +
		"Answer with only yes or no."
	// The verifier's DESCRIPTIVE_QUESTION, with which it turns an image
	// prompt into text with image_mode "descriptive" (and "contrastive"
	// without distractors); the GCS checks a box against that text
	verifierDescribeQuestion = "Describe the main person or object in this image as a short phrase for finding it in a low-resolution photo " +
		"taken by a drone from far above: what it is, then its most noticeable colors and any distinguishing features " +
		"large enough to see from far away. Leave out what it is doing, where it is and the background, and small " +
		"details such as logos, brands, text, small patterns, shoes and accessories. Answer with the phrase only, " +
		"under 12 words."
	// The verifier's context size: a request with another would make Ollama
	// reload the model the verifier is using
	verifierNumCtx = 4096
	// The verifier's live directory files (cliped/verifier.py LiveDir); an
	// image prompt wins over the text one
	livePromptText = "prompt.txt"
	// The verifier's text prompt, which it writes beside its prompt when the
	// prompt changes (cliped/verifier.py DESCRIPTION_FILE)
	liveDescription = "description.txt"
	// The image prompt the GCS writes, and its JPEG quality
	livePromptImage = "prompt.jpg"
	// The tracked box's detector class, written just before prompt.jpg; the
	// verifier takes it as the image's kind (cliped/verifier.py CLASS_FILE)
	liveClassFile = "class.txt"
	// A prompt a tracked box has matched is replaced only after this many
	// mismatches in a row: one section's pick of another object (a framing
	// the vision model misread) doesn't undo a prompt that was working
	replaceAfterMisses = 2
	promptQuality      = 95
	// The most distractors written for a section
	maxDistractors = 3
	// How much a distractor's box may overlap the target's in its frame
	// (intersection over union): more, and it is the target, or hides it
	maxDistractorIoU = 0.3
	// The crops: the box with this much context around it, at least
	// cropMinSide pixels, enlarged so the shorter side is enlargeMinSide
	checkContext   = 2.0
	describeMargin = 1.25
	cropMinSide    = 64
	enlargeMinSide = 224
	// The name of the mission auto_deploy compiles on the vehicle
	deployedMission = "inferred_mission"
)

var livePromptImages = []string{"prompt.jpg", "prompt.jpeg"}

// The verifier's distractor files (cliped/verifier.py DISTRACTOR_PATTERN)
var distractorFile = regexp.MustCompile(`^d(\d+)\.jpe?g$`)

// boxRef names a detection: its id in a sampled frame (both from 1).
type boxRef struct {
	Frame int `json:"frame"`
	Box   int `json:"box"`
}

// targetCheck records the box a Track section follows, and the verifier
// model's check of it.
type targetCheck struct {
	Frame int         `json:"frame"`
	Box   int         `json:"box"`
	Class string      `json:"class"`
	BBox  *[4]float64 `json:"bbox,omitempty"`
	// The verifier's prompt the box was checked against, and the file it
	// came from
	Prompt       string `json:"verifier_prompt,omitempty"`
	PromptSource string `json:"verifier_prompt_source,omitempty"`
	Answer       string `json:"verifier_answer,omitempty"`
	Match        *bool  `json:"verifier_match,omitempty"`
	// The crop of the box saved as the new image prompt, when it wasn't the
	// prompt (or there was none): its file beside the section's record, and
	// whether it was written to the live directory as prompt.jpg
	NewPromptImage string `json:"new_prompt_image,omitempty"`
	PromptWritten  bool   `json:"prompt_written,omitempty"`
	// A mismatch with a prompt that has matched, not yet acted on: the
	// mismatches in a row so far (the prompt is replaced at
	// replaceAfterMisses)
	Misses   int  `json:"misses,omitempty"`
	Deferred bool `json:"deferred,omitempty"`
	// The distractor boxes whose crops were written to the live directory,
	// their files there, and why any of the model's were skipped
	DistractorBoxes  []boxRef `json:"distractor_boxes,omitempty"`
	Distractors      []string `json:"distractors,omitempty"`
	DistractorErrors []string `json:"distractor_errors,omitempty"`
	Error            string   `json:"error,omitempty"`
	Elapsed          float64  `json:"elapsed,omitempty"`
}

// deployRecord is what auto_deploy did with a section's mission.
type deployRecord struct {
	Vehicle  string  `json:"vehicle"`
	Mission  string  `json:"mission"`
	Status   string  `json:"status"` // deployed or failed
	Error    string  `json:"error,omitempty"`
	Failsafe string  `json:"data_failsafe,omitempty"` // mission, or why it couldn't be set
	Elapsed  float64 `json:"elapsed"`
}

// Deployer sends missions to vehicles for auto_deploy.
type Deployer interface {
	// VehicleMission is the flow a mission planned with plan runs with on
	// its vehicle and the mission's DSL with its engines named as in that
	// flow (local.<engine>), as the vehicle compiles it; both empty without
	// a flow
	VehicleMission(ctx context.Context, plan mission.PlanningContext, g mission.Graph) ([]byte, string, error)
	// CompileOnVehicle has the vehicle compile the mission and load it
	CompileOnVehicle(ctx context.Context, vehicle, name string, flow, geojson []byte, dsl string) error
	// SetDataFailsafeMission has the vehicle start its loaded mission when
	// it loses its data link
	SetDataFailsafeMission(ctx context.Context, vehicle string) error
}

// DetectorOption is a detection engine a live run of a vehicle can use.
type DetectorOption struct {
	// Name is its name in the vehicle's flows: local.<engine> on the
	// vehicle's daemon, remote.<engine> on the GCS's
	Name string `json:"name"`
	// Engine is its full service name
	Engine string `json:"engine"`
	// Evidence is whether it runs on the GCS's daemon, where a run can read
	// its detections for the model
	Evidence bool `json:"evidence"`
}

// SetDetectorCatalog sets how the detection engines a vehicle's live run can
// use are listed.
func (s *Service) SetDetectorCatalog(list func(ctx context.Context, vehicle string) ([]DetectorOption, error)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.detectors = list
}

func (s *Service) listDetectors(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	s.mu.RLock()
	list := s.detectors
	s.mu.RUnlock()
	vehicle := r.URL.Query().Get("vehicle")
	if list == nil || vehicle == "" {
		reply(w, http.StatusOK, []DetectorOption{})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	options, err := list(ctx, vehicle)
	if err != nil {
		failHTTP(w, http.StatusServiceUnavailable, "Could not list the detection engines: "+err.Error())
		return
	}
	reply(w, http.StatusOK, options)
}

// SetDeployer sets how live runs with auto_deploy send their missions.
func (s *Service) SetDeployer(d Deployer) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.deployer = d
}

// StartLive starts observing producer, a camera of vehicle, as a live run
// planned with plan, without the Plan page; it returns the run's ID.
//
// localDetector, if not empty, is the run's Track detector, over the
// configured one (Settings.LocalDetector), by its name in plan's flow.
func (s *Service) StartLive(vehicle, producer, localDetector string, plan mission.PlanningContext) (string, error) {
	if !s.configured() {
		return "", errors.New("the vision model has no API key")
	}
	if !mediaAvailable() {
		return "", errors.New("FFmpeg and ffprobe are not installed")
	}
	r, err := s.start(startRequest{Source: "live", Vehicle: vehicle, Producer: producer, LocalDetector: localDetector, Plan: plan})
	if err != nil {
		return "", err
	}
	return r.meta.ID, nil
}

// trackedBox is the detection ref names in evidence.
func trackedBox(evidence *liveEvidence, ref *boxRef) (detectorLabel, error) {
	if evidence != nil {
		for _, frame := range evidence.Frames {
			if frame.Frame != ref.Frame {
				continue
			}
			for _, d := range frame.Detections {
				if d.ID == ref.Box {
					return d, nil
				}
			}
		}
	}
	return detectorLabel{}, fmt.Errorf("frame %d has no detection %d", ref.Frame, ref.Box)
}

// checkTarget checks the box ref (label) of the section's images against the
// verifier's prompt, and makes a crop of the box the verifier's image prompt
// if the verifier's model says it isn't the prompt.
func (s *Service) checkTarget(r *run, meta session, row section, ref boxRef, label detectorLabel, images []nl2dsl.Image) *targetCheck {
	start := time.Now()
	check := &targetCheck{Frame: ref.Frame, Box: ref.Box, Class: label.Class, BBox: label.Box}
	defer func() { check.Elapsed = time.Since(start).Seconds() }()
	fail := func(err error) *targetCheck {
		check.Error = err.Error()
		return check
	}
	if meta.Settings.Verifier == "" {
		return check
	}
	// One check at a time, so the mismatches in a row are counted in order
	r.target.mu.Lock()
	defer r.target.mu.Unlock()
	if label.Box == nil {
		return fail(errors.New("the detection has no box"))
	}
	if ref.Frame < 1 || ref.Frame > len(images) {
		return fail(fmt.Errorf("no image for frame %d", ref.Frame))
	}
	frame, err := jpeg.Decode(bytes.NewReader(images[ref.Frame-1].JPEG))
	if err != nil {
		return fail(fmt.Errorf("could not decode frame %d: %w", ref.Frame, err))
	}
	boxed, err := encodePNG(enlarge(cropAround(frame, *label.Box, checkContext, true), enlargeMinSide))
	if err != nil {
		return fail(err)
	}
	// Kept beside the section's record, for review
	_ = os.WriteFile(filepath.Join(r.sectionDir(row.Index), "target.png"), boxed, 0o600)

	ctx, cancel := context.WithTimeout(s.ctx, timeout(meta.Settings.RequestTimeout))
	defer cancel()
	model := newOllama(meta.Settings.Verifier)
	dir := meta.Settings.VerifierLiveDir
	if dir != "" {
		check.Prompt, check.PromptSource, err = s.livePrompt(ctx, model, dir)
		if err != nil {
			return fail(err)
		}
	}
	if check.Prompt != "" {
		check.Answer, err = model.ask(ctx, strings.ReplaceAll(verifierBoxQuestion, "{prompt}", check.Prompt), boxed, 8)
		if err != nil {
			return fail(err)
		}
		match, ok := parseAnswer(check.Answer)
		if !ok {
			return fail(errors.New("the verifier's model answered neither yes nor no"))
		}
		check.Match = &match
		if match {
			r.target.matched, r.target.misses = true, 0
			// The prompt stays; its distractors are added only if it has none,
			// since each change makes the verifier describe it again and
			// box_rerank start learning over
			if dir != "" && len(liveDistractors(dir)) == 0 {
				if err := s.writeDistractors(r, meta, row, ref, label, images, check, false); err != nil {
					return fail(err)
				}
			}
			return check
		}
		r.target.misses++
		check.Misses = r.target.misses
		if r.target.matched && r.target.misses < replaceAfterMisses {
			check.Deferred = true
			s.log.Info().Str("vehicle", meta.Vehicle).Int("misses", r.target.misses).
				Msg("inferred mission: the tracked object isn't the verifier's prompt, which has matched; kept for now")
			return check
		}
	}
	// Without the red box or much around it, so the verifier describes the
	// object alone
	tight, err := encodeJPEG(enlarge(cropAround(frame, *label.Box, describeMargin, false), enlargeMinSide))
	if err != nil {
		return fail(err)
	}
	// Kept beside the section's record too, for review
	check.NewPromptImage = "new_prompt.jpg"
	_ = os.WriteFile(filepath.Join(r.sectionDir(row.Index), check.NewPromptImage), tight, 0o600)
	if dir != "" {
		// The new target's distractors before the prompt, so the verifier
		// reads both at once; the old target's are set aside
		if err := s.writeDistractors(r, meta, row, ref, label, images, check, true); err != nil {
			return fail(err)
		}
		// Its class just before it, as the verifier's class prompt
		if err := writeWhole(dir, liveClassFile, []byte(label.Class+"\n")); err != nil {
			return fail(fmt.Errorf("could not write %s: %w", liveClassFile, err))
		}
		if err := writeLivePromptImage(dir, tight); err != nil {
			return fail(err)
		}
		check.PromptWritten = true
		r.target.matched, r.target.misses = false, 0
		s.log.Info().Str("vehicle", meta.Vehicle).Str("class", label.Class).
			Msg("inferred mission: the tracked object isn't the verifier's prompt; its crop is the new image prompt")
	}
	return check
}

// writeDistractors writes the crops of the section's distractor boxes (the
// vision model's, of the target's class, not overlapping the target) to the
// verifier's live directory as d<N>.jpg, setting aside the ones there first
// if replace is set, and records them in check. Boxes it can't use are
// recorded and skipped; an error is for the directory.
func (s *Service) writeDistractors(r *run, meta session, row section, target boxRef, label detectorLabel,
	images []nl2dsl.Image, check *targetCheck, replace bool) error {
	dir := meta.Settings.VerifierLiveDir
	var crops [][]byte
	skip := func(ref boxRef, why string) {
		check.DistractorErrors = append(check.DistractorErrors, fmt.Sprintf("frame %d box %d: %s", ref.Frame, ref.Box, why))
	}
	for _, ref := range row.DistractorBoxes {
		if len(crops) == maxDistractors {
			skip(ref, "too many distractors")
			continue
		}
		if ref == target {
			skip(ref, "it is the target")
			continue
		}
		d, err := trackedBox(row.Evidence, &ref)
		if err != nil {
			skip(ref, err.Error())
			continue
		}
		if d.Box == nil || !strings.EqualFold(strings.TrimSpace(d.Class), strings.TrimSpace(label.Class)) {
			skip(ref, fmt.Sprintf("not a %s with a box", label.Class))
			continue
		}
		if ref.Frame == target.Frame && label.Box != nil && iou(*d.Box, *label.Box) > maxDistractorIoU {
			skip(ref, "it overlaps the target")
			continue
		}
		if ref.Frame < 1 || ref.Frame > len(images) {
			skip(ref, "no image for its frame")
			continue
		}
		frame, err := jpeg.Decode(bytes.NewReader(images[ref.Frame-1].JPEG))
		if err != nil {
			skip(ref, "could not decode its frame")
			continue
		}
		crop, err := encodeJPEG(enlarge(cropAround(frame, *d.Box, describeMargin, false), enlargeMinSide))
		if err != nil {
			skip(ref, err.Error())
			continue
		}
		crops = append(crops, crop)
		check.DistractorBoxes = append(check.DistractorBoxes, ref)
	}
	if replace {
		if err := setAsideDistractors(dir); err != nil {
			return err
		}
	}
	for i, crop := range crops {
		// Kept beside the section's record too, for review
		_ = os.WriteFile(filepath.Join(r.sectionDir(row.Index), fmt.Sprintf("distractor_%d.jpg", i+1)), crop, 0o600)
		name, err := writeLiveDistractor(dir, crop)
		if err != nil {
			return err
		}
		check.Distractors = append(check.Distractors, name)
	}
	if len(crops) > 0 {
		s.log.Info().Str("vehicle", meta.Vehicle).Strs("distractors", check.Distractors).
			Msg("inferred mission: wrote the tracked object's distractors for the verifier")
	}
	return nil
}

// iou is the intersection over union of the boxes a and b.
func iou(a, b [4]float64) float64 {
	w := math.Min(a[2], b[2]) - math.Max(a[0], b[0])
	h := math.Min(a[3], b[3]) - math.Max(a[1], b[1])
	if w <= 0 || h <= 0 {
		return 0
	}
	inter := w * h
	union := (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
	if union <= 0 {
		return 0
	}
	return inter / union
}

// liveDistractors is the names of the distractor files in dir.
func liveDistractors(dir string) []string {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var names []string
	for _, e := range entries {
		if !e.IsDir() && distractorFile.MatchString(e.Name()) {
			names = append(names, e.Name())
		}
	}
	return names
}

// setAsideDistractors renames dir's distractors to replaced-<stamp>-<name>.
func setAsideDistractors(dir string) error {
	stamp := time.Now().UTC().Format("20060102T150405")
	for _, name := range liveDistractors(dir) {
		err := os.Rename(filepath.Join(dir, name), filepath.Join(dir, "replaced-"+stamp+"-"+name))
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("could not set aside the distractor %s: %w", name, err)
		}
	}
	return nil
}

// writeLiveDistractor writes the JPEG image to dir as d<N>.jpg, N one more
// than the highest there, whole, and returns its name.
func writeLiveDistractor(dir string, jpegData []byte) (string, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", fmt.Errorf("could not make the verifier's live directory: %w", err)
	}
	next := 1
	for _, name := range liveDistractors(dir) {
		if n, err := strconv.Atoi(distractorFile.FindStringSubmatch(name)[1]); err == nil && n >= next {
			next = n + 1
		}
	}
	name := fmt.Sprintf("d%d.jpg", next)
	if err := writeWhole(dir, name, jpegData); err != nil {
		return "", fmt.Errorf("could not write the distractor %s: %w", name, err)
	}
	return name, nil
}

// writeWhole writes data to dir/name through a temporary file renamed into
// place, so a reader never sees half of it.
func writeWhole(dir, name string, data []byte) error {
	temp, err := os.CreateTemp(dir, ".write-*")
	if err != nil {
		return err
	}
	defer os.Remove(temp.Name())
	_, err = temp.Write(data)
	if closeErr := temp.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(temp.Name(), filepath.Join(dir, name))
	}
	return err
}

// livePrompt is the verifier's prompt in its live directory dir, and the file
// it was read from: the verifier's own text (description.txt) if it wrote it
// since the prompt last changed; else the text of prompt.txt, or the model's
// description of the image prompt (as the verifier describes it with
// image_mode "descriptive"); "" if there is none.
func (s *Service) livePrompt(ctx context.Context, model ollama, dir string) (string, string, error) {
	var newest os.FileInfo
	for _, name := range livePromptImages {
		if info, err := os.Stat(filepath.Join(dir, name)); err == nil && (newest == nil || info.ModTime().After(newest.ModTime())) {
			newest = info
		}
	}
	prompt := newest
	if prompt == nil {
		prompt, _ = os.Stat(filepath.Join(dir, livePromptText))
	}
	if described, err := os.Stat(filepath.Join(dir, liveDescription)); err == nil && prompt != nil &&
		!described.ModTime().Before(prompt.ModTime()) {
		if data, err := os.ReadFile(filepath.Join(dir, liveDescription)); err == nil {
			if text := strings.TrimSpace(string(data)); text != "" {
				return text, liveDescription, nil
			}
		}
	}
	if newest == nil {
		data, err := os.ReadFile(filepath.Join(dir, livePromptText))
		if errors.Is(err, os.ErrNotExist) {
			return "", "", nil
		} else if err != nil {
			return "", "", fmt.Errorf("could not read the verifier's prompt: %w", err)
		}
		return strings.TrimSpace(string(data)), livePromptText, nil
	}
	path := filepath.Join(dir, newest.Name())
	key := fmt.Sprintf("%s|%s|%d|%d", model.model, path, newest.ModTime().UnixNano(), newest.Size())
	s.mu.RLock()
	described, ok := s.described[key]
	s.mu.RUnlock()
	if ok {
		return described, newest.Name(), nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return "", "", fmt.Errorf("could not read the verifier's prompt: %w", err)
	}
	img, _, err := image.Decode(bytes.NewReader(data))
	if err != nil {
		return "", "", fmt.Errorf("could not decode the verifier's prompt %s: %w", newest.Name(), err)
	}
	encoded, err := encodePNG(img)
	if err != nil {
		return "", "", err
	}
	reply, err := model.ask(ctx, verifierDescribeQuestion, encoded, 40)
	if err != nil {
		return "", "", err
	}
	described = cleanDescription(reply)
	s.mu.Lock()
	if s.described == nil {
		s.described = map[string]string{}
	}
	s.described[key] = described
	s.mu.Unlock()
	return described, newest.Name(), nil
}

// writeLivePromptImage makes the JPEG image the verifier's prompt:
// prompt.jpg replaced whole, and prompt.jpeg, which would win over it if it
// were newer, renamed aside. An image wins over prompt.txt, which is left
// as it is. The directory is made if the verifier hasn't made it yet (it
// does when it first reads its prompt, which may be after the first
// section).
func writeLivePromptImage(dir string, jpegData []byte) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("could not make the verifier's live directory: %w", err)
	}
	stamp := time.Now().UTC().Format("20060102T150405")
	for _, name := range livePromptImages {
		if name == livePromptImage {
			continue
		}
		err := os.Rename(filepath.Join(dir, name), filepath.Join(dir, "replaced-"+stamp+"-"+name))
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("could not set aside the image prompt %s: %w", name, err)
		}
	}
	temp, err := os.CreateTemp(dir, ".prompt-*.jpg")
	if err != nil {
		return fmt.Errorf("could not write the verifier's prompt: %w", err)
	}
	defer os.Remove(temp.Name())
	_, err = temp.Write(jpegData)
	if closeErr := temp.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		// Renamed into place whole, so the verifier never reads half of it
		err = os.Rename(temp.Name(), filepath.Join(dir, livePromptImage))
	}
	if err != nil {
		return fmt.Errorf("could not write the verifier's prompt: %w", err)
	}
	return nil
}

// deploy has the vehicle compile the section's mission (drafted, planned with
// plan) and load it, then start it when its data link fails.
func (s *Service) deploy(meta session, plan mission.PlanningContext, drafted mission.Draft) *deployRecord {
	start := time.Now()
	vehicle := meta.Plan.Vehicle
	record := &deployRecord{Vehicle: vehicle, Mission: deployedMission, Status: "failed"}
	defer func() { record.Elapsed = time.Since(start).Seconds() }()
	s.mu.RLock()
	deployer := s.deployer
	s.mu.RUnlock()
	switch {
	case deployer == nil:
		record.Error = "The GCS has no daemon to send missions through."
		return record
	case vehicle == "":
		record.Error = "No vehicle is selected."
		return record
	}
	ctx, cancel := context.WithTimeout(s.ctx, timeout(meta.Settings.GenerationTimeout))
	defer cancel()
	// The drafted DSL names engines by host (heisenberg.box_rerank), which
	// the vehicle doesn't know them by: it gets the mission with the names
	// in its flow (local.box_rerank)
	var flow []byte
	dsl := drafted.DSL
	if drafted.Graph != nil {
		vehicleFlow, vehicleDSL, err := deployer.VehicleMission(ctx, plan, *drafted.Graph)
		if err != nil {
			record.Error = "Could not resolve the mission for the vehicle: " + err.Error()
			return record
		}
		if vehicleDSL != "" {
			flow, dsl = vehicleFlow, vehicleDSL
		}
	}
	var geojson []byte
	if plan.GeoJSON != "" {
		geojson = []byte(plan.GeoJSON)
	}
	if err := deployer.CompileOnVehicle(ctx, vehicle, deployedMission, flow, geojson, dsl); err != nil {
		record.Error = err.Error()
		return record
	}
	record.Status = "deployed"
	if err := deployer.SetDataFailsafeMission(ctx, vehicle); err != nil {
		record.Failsafe = err.Error()
	} else {
		record.Failsafe = "mission"
	}
	s.log.Info().Str("vehicle", vehicle).Str("data_failsafe", record.Failsafe).Msg("inferred mission: compiled and loaded on the vehicle")
	return record
}

// ollama asks an Ollama model about one image, as the cliped verifier does.
type ollama struct {
	host, model string
}

// newOllama uses the Ollama at OLLAMA_HOST, else on this machine.
func newOllama(model string) ollama {
	host := strings.TrimSpace(os.Getenv("OLLAMA_HOST"))
	if host == "" {
		host = "localhost:11434"
	}
	if !strings.Contains(host, "://") {
		host = "http://" + host
	}
	if u, err := url.Parse(host); err == nil {
		// A server's bind address, not one to connect to
		if name := u.Hostname(); name == "0.0.0.0" || name == "::" {
			port := u.Port()
			if port == "" {
				port = "11434"
			}
			u.Host = "localhost:" + port
		}
		host = u.String()
	}
	return ollama{host: strings.TrimRight(host, "/"), model: model}
}

func (o ollama) ask(ctx context.Context, question string, png []byte, maxTokens int) (string, error) {
	body, err := json.Marshal(map[string]any{
		"model": o.model, "stream": false, "keep_alive": -1,
		"options":  map[string]any{"temperature": 0, "num_predict": maxTokens, "num_ctx": verifierNumCtx},
		"messages": []map[string]any{{"role": "user", "content": question, "images": []string{base64.StdEncoding.EncodeToString(png)}}},
	})
	if err != nil {
		return "", err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, o.host+"/api/chat", bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("the verifier's model is unavailable: %w", err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return "", fmt.Errorf("the verifier's model is unavailable: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("the verifier's model failed: HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(data)))
	}
	var reply struct {
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
	}
	if err := json.Unmarshal(data, &reply); err != nil {
		return "", fmt.Errorf("the verifier's model gave an invalid reply: %w", err)
	}
	return strings.TrimSpace(reply.Message.Content), nil
}

// parseAnswer is a yes or a no; ok is false for anything else.
func parseAnswer(answer string) (yes, ok bool) {
	word := strings.ToLower(strings.TrimLeft(strings.TrimSpace(answer), "*\"'`"))
	switch {
	case strings.HasPrefix(word, "yes"):
		return true, true
	case strings.HasPrefix(word, "no"):
		return false, true
	}
	return false, false
}

// cleanDescription is the model's description as one line, without quotes
// or a full stop.
func cleanDescription(text string) string {
	return strings.Trim(strings.Join(strings.Fields(text), " "), "\"'`*. ")
}

// cropAround is the square around box (fractions of img's sides), its side
// scale times the box's longer side and at least cropMinSide pixels, shifted
// into img; with outline, the box drawn in red, as box_rerank draws it.
func cropAround(img image.Image, box [4]float64, scale float64, outline bool) *image.RGBA {
	bounds := img.Bounds()
	w, h := float64(bounds.Dx()), float64(bounds.Dy())
	left, top, right, bottom := box[0]*w, box[1]*h, box[2]*w, box[3]*h
	side := math.Min(math.Max(math.Max(right-left, bottom-top)*scale, cropMinSide), math.Min(w, h))
	x0 := math.Min(math.Max(math.Round((left+right-side)/2), 0), w-side)
	y0 := math.Min(math.Max(math.Round((top+bottom-side)/2), 0), h-side)
	n := int(math.Round(side))
	out := image.NewRGBA(image.Rect(0, 0, n, n))
	for y := range n {
		for x := range n {
			out.Set(x, y, img.At(bounds.Min.X+int(x0)+x, bounds.Min.Y+int(y0)+y))
		}
	}
	if outline {
		red := color.RGBA{255, 0, 0, 255}
		thickness := max(1, int(math.Round(side/100)))
		l, t := int(math.Round(left-x0)), int(math.Round(top-y0))
		r, b := int(math.Round(right-x0))-1, int(math.Round(bottom-y0))-1
		for i := range thickness {
			for x := l; x <= r; x++ {
				setIn(out, x, t+i, red)
				setIn(out, x, b-i, red)
			}
			for y := t; y <= b; y++ {
				setIn(out, l+i, y, red)
				setIn(out, r-i, y, red)
			}
		}
	}
	return out
}

func setIn(img *image.RGBA, x, y int, c color.RGBA) {
	if image.Pt(x, y).In(img.Rect) {
		img.SetRGBA(x, y, c)
	}
}

// enlarge is img scaled up (bilinear), keeping its aspect ratio, so its
// shorter side is at least minSide; as is if it already is.
func enlarge(img *image.RGBA, minSide int) *image.RGBA {
	w, h := img.Rect.Dx(), img.Rect.Dy()
	if w == 0 || h == 0 || min(w, h) >= minSide {
		return img
	}
	scale := float64(minSide) / float64(min(w, h))
	nw, nh := int(math.Round(float64(w)*scale)), int(math.Round(float64(h)*scale))
	out := image.NewRGBA(image.Rect(0, 0, nw, nh))
	for y := range nh {
		sy := math.Min(math.Max((float64(y)+0.5)/scale-0.5, 0), float64(h-1))
		y0 := int(sy)
		y1, fy := min(y0+1, h-1), sy-float64(y0)
		for x := range nw {
			sx := math.Min(math.Max((float64(x)+0.5)/scale-0.5, 0), float64(w-1))
			x0 := int(sx)
			x1, fx := min(x0+1, w-1), sx-float64(x0)
			var px [4]uint8
			for c := range 4 {
				at := func(x, y int) float64 { return float64(img.Pix[y*img.Stride+x*4+c]) }
				top := at(x0, y0)*(1-fx) + at(x1, y0)*fx
				bottom := at(x0, y1)*(1-fx) + at(x1, y1)*fx
				px[c] = uint8(math.Round(top*(1-fy) + bottom*fy))
			}
			copy(out.Pix[y*out.Stride+x*4:], px[:])
		}
	}
	return out
}

func encodeJPEG(img image.Image) ([]byte, error) {
	var buf bytes.Buffer
	if err := jpeg.Encode(&buf, img, &jpeg.Options{Quality: promptQuality}); err != nil {
		return nil, fmt.Errorf("could not encode the crop: %w", err)
	}
	return buf.Bytes(), nil
}

func encodePNG(img image.Image) ([]byte, error) {
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		return nil, fmt.Errorf("could not encode the crop: %w", err)
	}
	return buf.Bytes(), nil
}
