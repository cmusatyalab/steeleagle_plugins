package inferredmission

// When a live section's recommendation is Track, the vision model names the
// detection box of the object the pilot is following (target_box). That
// box's detector class becomes the Track class, and the box is checked
// against the cliped verifier's prompt with the verifier's own model: if the
// model says it isn't the prompt, it describes the box, and the description
// becomes the verifier's prompt through its live directory, which box_rerank
// then follows. With auto_deploy, the section's compiled mission is sent to
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
	verifierDescribeQuestion = "Describe the person or object in this image as a short phrase for an image search that must find it " +
		"from a drone, e.g. 'a person in a red shirt and green shorts' or 'a quadruped robot dog'. Describe only " +
		"the person or object itself, not the ground, background or what it is doing; only colors and shapes " +
		"visible from far above. Answer with the phrase only, under 15 words."
	// The verifier's context size: a request with another would make Ollama
	// reload the model the verifier is using
	verifierNumCtx = 4096
	// The verifier's live directory files (cliped/verifier.py LiveDir); an
	// image prompt wins over the text one
	livePromptText = "prompt.txt"
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
	// The description of the box, when it wasn't the prompt (or there was
	// none), and whether it was written to the live directory
	NewPrompt     string  `json:"new_prompt,omitempty"`
	PromptWritten bool    `json:"prompt_written,omitempty"`
	Error         string  `json:"error,omitempty"`
	Elapsed       float64 `json:"elapsed,omitempty"`
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
// verifier's prompt, and replaces the prompt with a description of the box
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
			return check
		}
	}
	// Described without the red box or much around it, so the description
	// is of the object alone
	tight, err := encodePNG(enlarge(cropAround(frame, *label.Box, describeMargin, false), enlargeMinSide))
	if err != nil {
		return fail(err)
	}
	description, err := model.ask(ctx, verifierDescribeQuestion, tight, 40)
	if err != nil {
		return fail(err)
	}
	if check.NewPrompt = cleanDescription(description); check.NewPrompt == "" {
		return fail(errors.New("the verifier's model gave no description"))
	}
	if dir != "" {
		if err := writeLivePrompt(dir, check.NewPrompt); err != nil {
			return fail(err)
		}
		check.PromptWritten = true
		s.log.Info().Str("vehicle", meta.Vehicle).Str("prompt", check.NewPrompt).Str("class", label.Class).
			Msg("inferred mission: the tracked object isn't the verifier's prompt; replaced it")
	}
	return check
}

// livePrompt is the verifier's prompt in its live directory dir: the text of
// prompt.txt, or the model's description of the image prompt (the verifier
// describes it the same way), and the file's name; "" if there is none.
func (s *Service) livePrompt(ctx context.Context, model ollama, dir string) (string, string, error) {
	var newest os.FileInfo
	for _, name := range livePromptImages {
		if info, err := os.Stat(filepath.Join(dir, name)); err == nil && (newest == nil || info.ModTime().After(newest.ModTime())) {
			newest = info
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

// writeLivePrompt makes text the verifier's prompt: prompt.txt replaced
// whole, then any image prompt, which would win over it, renamed aside.
// The directory is made if the verifier hasn't made it yet (it does when it
// first reads its prompt, which may be after the first section).
func writeLivePrompt(dir, text string) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("could not make the verifier's live directory: %w", err)
	}
	temp, err := os.CreateTemp(dir, ".prompt-*.txt")
	if err != nil {
		return fmt.Errorf("could not write the verifier's prompt: %w", err)
	}
	defer os.Remove(temp.Name())
	_, err = temp.WriteString(text + "\n")
	if closeErr := temp.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(temp.Name(), filepath.Join(dir, livePromptText))
	}
	if err != nil {
		return fmt.Errorf("could not write the verifier's prompt: %w", err)
	}
	stamp := time.Now().UTC().Format("20060102T150405")
	for _, name := range livePromptImages {
		err := os.Rename(filepath.Join(dir, name), filepath.Join(dir, "replaced-"+stamp+"-"+name))
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("could not set aside the image prompt %s: %w", name, err)
		}
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

func encodePNG(img image.Image) ([]byte, error) {
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		return nil, fmt.Errorf("could not encode the crop: %w", err)
	}
	return buf.Bytes(), nil
}
