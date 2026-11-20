package inferredmission

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"image"
	"image/color"
	"image/jpeg"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/nl2dsl"
	"github.com/rs/zerolog"
)

func TestTheModelsTargetBoxIsReadOnlyForTrack(t *testing.T) {
	var row section
	reply := `{"recommendation": "Track the dog, then return home. If the battery drops to 30 percent, return home.",
		"evidence": "The framing follows the dog.", "target_box": {"frame": 3, "box": 2}}`
	if _, err := row.readVisionRecommendation("v7", reply); err != nil || row.TargetBox == nil || *row.TargetBox != (boxRef{3, 2}) {
		t.Fatalf("expected box 2 of frame 3, got %+v (%v)", row.TargetBox, err)
	}
	row = section{}
	reply = `{"recommendation": "Wait 30 seconds, then return home.", "evidence": "Unclear.", "target_box": {"frame": 3, "box": 2}}`
	if _, err := row.readVisionRecommendation("v7", reply); err != nil || row.TargetBox != nil {
		t.Fatalf("a box is only for tracking, got %+v (%v)", row.TargetBox, err)
	}
}

func TestDistractorBoxesAreReadWithTheTargetBox(t *testing.T) {
	var row section
	reply := `{"recommendation": "Track the person, then return home. If the battery drops to 30 percent, return home.",
		"evidence": "The framing follows one person.", "target_box": {"frame": 3, "box": 2},
		"distractor_boxes": [{"frame": 3, "box": 1}, {"frame": 1, "box": 4}]}`
	if _, err := row.readVisionRecommendation("v7", reply); err != nil || len(row.DistractorBoxes) != 2 ||
		row.DistractorBoxes[1] != (boxRef{1, 4}) {
		t.Fatalf("expected two distractors, got %+v (%v)", row.DistractorBoxes, err)
	}
}

func distractorRow() section {
	box := func(id int, class string, x float64) detectorLabel {
		return detectorLabel{ID: id, Class: class, Box: &[4]float64{x, 0.4, x + 0.1, 0.6}}
	}
	return section{
		Evidence: &liveEvidence{Frames: []frameEvidence{{Frame: 1, Detections: []detectorLabel{
			box(1, "person", 0.1), box(2, "person", 0.4), box(3, "car", 0.7), box(4, "Person", 0.42)}}}},
		DistractorBoxes: []boxRef{{1, 1}, {1, 2}, {1, 3}, {1, 4}, {1, 9}},
	}
}

func TestANewTargetsDistractorsReplaceTheOldOnes(t *testing.T) {
	fakeOllama(t, "No.", "unused")
	dir := t.TempDir()
	for _, name := range []string{"prompt.txt", "d1.jpg", "d7.jpg"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte("old"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	s := &Service{ctx: t.Context(), log: zerolog.Nop()}
	meta := session{Settings: DefaultSettings()}
	meta.Settings.Verifier, meta.Settings.VerifierLiveDir = "qwen", dir
	r := &run{dir: t.TempDir()}
	if err := os.MkdirAll(r.sectionDir(0), 0o755); err != nil {
		t.Fatal(err)
	}
	row := distractorRow()
	label := row.Evidence.Frames[0].Detections[1]
	check := s.checkTarget(r, meta, row, boxRef{1, 2}, label, targetFrame(t))
	// Box 1 only: 2 is the target, 3 a car, 4 overlaps the target, 9 isn't there
	if check.Error != "" || !check.PromptWritten || len(check.DistractorBoxes) != 1 ||
		check.DistractorBoxes[0] != (boxRef{1, 1}) || len(check.DistractorErrors) != 4 {
		t.Fatalf("expected box 1 alone as a distractor, got %+v", check)
	}
	if got := liveDistractors(dir); len(got) != 1 || got[0] != "d1.jpg" || check.Distractors[0] != "d1.jpg" {
		t.Fatalf("expected the old distractors set aside and d1.jpg written, got %v", got)
	}
	if data, _ := os.ReadFile(filepath.Join(dir, "d1.jpg")); bytes.Equal(data, []byte("old")) {
		t.Fatal("d1.jpg is still the old distractor")
	}
}

func TestAKeptPromptGetsDistractorsOnlyIfItHasNone(t *testing.T) {
	fakeOllama(t, "Yes", "unused")
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "prompt.txt"), []byte("a person"), 0o600); err != nil {
		t.Fatal(err)
	}
	s := &Service{ctx: t.Context(), log: zerolog.Nop()}
	meta := session{Settings: DefaultSettings()}
	meta.Settings.Verifier, meta.Settings.VerifierLiveDir = "qwen", dir
	r := &run{dir: t.TempDir()}
	if err := os.MkdirAll(r.sectionDir(0), 0o755); err != nil {
		t.Fatal(err)
	}
	row := distractorRow()
	label := row.Evidence.Frames[0].Detections[1]
	for i := 0; i < 2; i++ {
		s.checkTarget(r, meta, row, boxRef{1, 2}, label, targetFrame(t))
	}
	if got := liveDistractors(dir); len(got) != 1 {
		t.Fatalf("expected one distractor written once, got %v", got)
	}
}

func TestAPromptThatMatchedIsReplacedOnlyAfterTwoMismatchesInARow(t *testing.T) {
	answer := "Yes"
	fakeOllamaFunc(t, func() string { return answer }, "unused")
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "prompt.txt"), []byte("a person in black"), 0o600); err != nil {
		t.Fatal(err)
	}
	s := &Service{ctx: t.Context(), log: zerolog.Nop()}
	meta := session{Settings: DefaultSettings()}
	meta.Settings.Verifier, meta.Settings.VerifierLiveDir = "qwen", dir
	r := &run{dir: t.TempDir()}
	if err := os.MkdirAll(r.sectionDir(0), 0o755); err != nil {
		t.Fatal(err)
	}
	row := distractorRow()
	label := row.Evidence.Frames[0].Detections[1]
	check := func() *targetCheck { return s.checkTarget(r, meta, row, boxRef{1, 2}, label, targetFrame(t)) }
	if c := check(); c.Match == nil || !*c.Match {
		t.Fatalf("expected a match, got %+v", c)
	}
	answer = "No"
	if c := check(); !c.Deferred || c.PromptWritten || c.Misses != 1 {
		t.Fatalf("expected the first mismatch deferred, got %+v", c)
	}
	// A match in between starts the count over
	answer = "Yes"
	check()
	answer = "No"
	if c := check(); !c.Deferred {
		t.Fatalf("expected the count started over, got %+v", c)
	}
	if c := check(); c.Deferred || !c.PromptWritten || c.Misses != 2 {
		t.Fatalf("expected the second mismatch in a row to replace the prompt, got %+v", c)
	}
	if data, err := os.ReadFile(filepath.Join(dir, "class.txt")); err != nil || string(data) != "person\n" {
		t.Fatalf("expected class.txt to hold the class, got %q (%v)", data, err)
	}
	// The new prompt hasn't matched: a mismatch replaces it at once
	if c := check(); c.Deferred || !c.PromptWritten {
		t.Fatalf("expected a prompt that never matched replaced at once, got %+v", c)
	}
}

func TestTrackedBoxNamesADetectionOfTheEvidence(t *testing.T) {
	evidence := &liveEvidence{Frames: []frameEvidence{
		{Frame: 1, Detections: []detectorLabel{{ID: 1, Class: "person"}}},
		{Frame: 3, Detections: []detectorLabel{{ID: 1, Class: "person"}, {ID: 2, Class: "robotic dog"}}},
	}}
	if got, err := trackedBox(evidence, &boxRef{3, 2}); err != nil || got.Class != "robotic dog" {
		t.Fatalf("expected the robotic dog, got %+v (%v)", got, err)
	}
	for _, ref := range []boxRef{{3, 3}, {2, 1}} {
		if _, err := trackedBox(evidence, &ref); err == nil {
			t.Fatalf("%+v names no detection", ref)
		}
	}
}

func TestCropsAreSquareAroundTheBoxAndEnlarged(t *testing.T) {
	frame := image.NewRGBA(image.Rect(0, 0, 640, 360))
	// A 32 px box near the right edge: twice its side is under the minimum
	crop := cropAround(frame, [4]float64{0.95, 0.5, 1, 0.58}, checkContext, true)
	if crop.Rect.Dx() != cropMinSide || crop.Rect.Dy() != cropMinSide {
		t.Fatalf("expected a %d px square, got %v", cropMinSide, crop.Rect)
	}
	// Shifted into the frame, so the box is at its right edge, drawn in red
	if c := crop.RGBAAt(cropMinSide-1, cropMinSide/2); c != (color.RGBA{255, 0, 0, 255}) {
		t.Fatalf("expected the box's right side in red, got %v", c)
	}
	big := enlarge(crop, enlargeMinSide)
	if big.Rect.Dx() != enlargeMinSide || big.Rect.Dy() != enlargeMinSide {
		t.Fatalf("expected %d px, got %v", enlargeMinSide, big.Rect)
	}
}

func TestWritingAnImagePromptReplacesPromptJPGAndSetsAsidePromptJPEG(t *testing.T) {
	dir := t.TempDir()
	for name, data := range map[string]string{"prompt.jpg": "old jpg", "prompt.jpeg": "old jpeg", "prompt.txt": "a person\n"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(data), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := writeLivePromptImage(dir, []byte("new jpeg")); err != nil {
		t.Fatal(err)
	}
	if data, _ := os.ReadFile(filepath.Join(dir, "prompt.jpg")); string(data) != "new jpeg" {
		t.Fatalf("got prompt.jpg %q", data)
	}
	if _, err := os.Stat(filepath.Join(dir, "prompt.jpeg")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("prompt.jpeg, which could win over prompt.jpg, is still there")
	}
	if aside, _ := filepath.Glob(filepath.Join(dir, "replaced-*-prompt.jpeg")); len(aside) != 1 {
		t.Fatalf("expected prompt.jpeg kept aside, got %v", aside)
	}
	// The text prompt is left as it is: the image wins over it
	if text, _ := os.ReadFile(filepath.Join(dir, "prompt.txt")); string(text) != "a person\n" {
		t.Fatalf("got prompt.txt %q", text)
	}
	if temps, _ := filepath.Glob(filepath.Join(dir, ".prompt-*")); len(temps) != 0 {
		t.Fatalf("left temporary files %v", temps)
	}
}

// fakeOllama answers the box question with answer, and describes with
// description; it records the questions asked.
func fakeOllama(t *testing.T, answer, description string) *[]string {
	return fakeOllamaFunc(t, func() string { return answer }, description)
}

// fakeOllamaFunc is fakeOllama with the box answer from answer() each time.
func fakeOllamaFunc(t *testing.T, answer func() string, description string) *[]string {
	var mu sync.Mutex
	var asked []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Model    string
			Options  map[string]any
			Messages []struct{ Content string }
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil || r.URL.Path != "/api/chat" || body.Model != "qwen" ||
			body.Options["num_ctx"] != float64(verifierNumCtx) {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		question := body.Messages[0].Content
		mu.Lock()
		asked = append(asked, question)
		mu.Unlock()
		reply := description
		if strings.Contains(question, "red box") {
			reply = answer()
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"message": map[string]string{"content": reply}})
	}))
	t.Cleanup(server.Close)
	t.Setenv("OLLAMA_HOST", server.URL)
	return &asked
}

func targetFrame(t *testing.T) []nl2dsl.Image {
	var buf bytes.Buffer
	if err := jpeg.Encode(&buf, image.NewRGBA(image.Rect(0, 0, 320, 180)), nil); err != nil {
		t.Fatal(err)
	}
	return []nl2dsl.Image{{JPEG: buf.Bytes()}}
}

func TestATrackedObjectThatIsntThePromptBecomesThePrompt(t *testing.T) {
	asked := fakeOllama(t, "No.", "unused")
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "prompt.txt"), []byte("a person in a red shirt\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	s := &Service{ctx: t.Context(), log: zerolog.Nop()}
	meta := session{Settings: DefaultSettings()}
	meta.Settings.Verifier, meta.Settings.VerifierLiveDir = "qwen", dir
	r := &run{dir: t.TempDir()}
	if err := os.MkdirAll(r.sectionDir(0), 0o755); err != nil {
		t.Fatal(err)
	}
	label := detectorLabel{ID: 2, Class: "robotic dog", Box: &[4]float64{0.4, 0.4, 0.5, 0.6}}
	check := s.checkTarget(r, meta, section{}, boxRef{1, 2}, label, targetFrame(t))
	if check.Error != "" || check.Prompt != "a person in a red shirt" || check.Match == nil || *check.Match ||
		check.NewPromptImage != "new_prompt.jpg" || !check.PromptWritten {
		t.Fatalf("expected the prompt replaced, got %+v", check)
	}
	// Only the box question: the verifier describes the crop itself
	if len(*asked) != 1 || !strings.Contains((*asked)[0], "red box a person in a red shirt?") {
		t.Fatalf("expected only the box question with the prompt, got %q", *asked)
	}
	data, err := os.ReadFile(filepath.Join(dir, "prompt.jpg"))
	if err != nil {
		t.Fatal(err)
	}
	img, err := jpeg.Decode(bytes.NewReader(data))
	if err != nil || img.Bounds().Dx() < enlargeMinSide {
		t.Fatalf("expected a JPEG crop at least %d px, got %v (%v)", enlargeMinSide, img.Bounds(), err)
	}
	if saved, _ := os.ReadFile(filepath.Join(r.sectionDir(0), "new_prompt.jpg")); !bytes.Equal(saved, data) {
		t.Fatal("the crop isn't kept beside the section's record")
	}
}

func TestTheVerifiersOwnTextIsUsedWhenItIsNewerThanThePrompt(t *testing.T) {
	asked := fakeOllama(t, "Yes", "a person in a gray shirt")
	dir := t.TempDir()
	write := func(name, text string, at time.Time) {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.Chtimes(filepath.Join(dir, name), at, at); err != nil {
			t.Fatal(err)
		}
	}
	now := time.Now()
	s := &Service{ctx: t.Context(), log: zerolog.Nop()}
	model := newOllama("qwen")
	// The verifier's text of an older prompt: the GCS describes the new one
	write("description.txt", "person in black hoodie and blue jeans\n", now.Add(-time.Minute))
	write("prompt.jpg", string(targetFrame(t)[0].JPEG), now.Add(-30*time.Second))
	if text, source, err := s.livePrompt(t.Context(), model, dir); err != nil || source != "prompt.jpg" ||
		text != "a person in a gray shirt" || len(*asked) != 1 {
		t.Fatalf("expected the GCS's description of prompt.jpg, got %q from %s (%v)", text, source, err)
	}
	// Written since the prompt: the verifier's text, without asking the model
	write("description.txt", "person in black hoodie and blue jeans\n", now)
	if text, source, err := s.livePrompt(t.Context(), model, dir); err != nil || source != "description.txt" ||
		text != "person in black hoodie and blue jeans" || len(*asked) != 1 {
		t.Fatalf("expected the verifier's text, got %q from %s (%v)", text, source, err)
	}
}

func TestATrackedObjectThatIsThePromptKeepsIt(t *testing.T) {
	asked := fakeOllama(t, "Yes", "unused")
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "prompt.txt"), []byte("a quadruped robot dog"), 0o600); err != nil {
		t.Fatal(err)
	}
	s := &Service{ctx: t.Context(), log: zerolog.Nop()}
	meta := session{Settings: DefaultSettings()}
	meta.Settings.Verifier, meta.Settings.VerifierLiveDir = "qwen", dir
	label := detectorLabel{ID: 1, Class: "robotic dog", Box: &[4]float64{0.4, 0.4, 0.5, 0.6}}
	check := s.checkTarget(&run{dir: t.TempDir()}, meta, section{}, boxRef{1, 1}, label, targetFrame(t))
	if check.Error != "" || check.Match == nil || !*check.Match || check.NewPromptImage != "" || len(*asked) != 1 {
		t.Fatalf("expected the prompt kept, got %+v after %q", check, *asked)
	}
	if text, _ := os.ReadFile(filepath.Join(dir, "prompt.txt")); string(text) != "a quadruped robot dog" {
		t.Fatalf("the prompt changed: %q", text)
	}
}

type fakeDeployer struct {
	compileErr error
	calls      []string
}

func (d *fakeDeployer) VehicleMission(context.Context, mission.PlanningContext, mission.Graph) ([]byte, string, error) {
	d.calls = append(d.calls, "flow")
	return []byte("flow"), "vehicle dsl", nil
}

func (d *fakeDeployer) CompileOnVehicle(_ context.Context, vehicle, name string, flow, _ []byte, dsl string) error {
	d.calls = append(d.calls, "compile "+vehicle+" "+name+" "+string(flow)+" "+dsl)
	return d.compileErr
}

func (d *fakeDeployer) SetDataFailsafeMission(_ context.Context, vehicle string) error {
	d.calls = append(d.calls, "failsafe "+vehicle)
	return nil
}

func TestDeployCompilesOnTheVehicleThenSetsTheFailsafe(t *testing.T) {
	d := &fakeDeployer{}
	s := &Service{ctx: t.Context(), log: zerolog.Nop()}
	s.SetDeployer(d)
	meta := session{Settings: DefaultSettings(), Plan: mission.PlanningContext{Vehicle: "10.0.0.2.alpha"}}
	record := s.deploy(meta, meta.Plan, mission.Draft{DSL: "dsl", Graph: &mission.Graph{}, Status: "compiled"})
	// The vehicle's DSL, with its engines named as in its flow, not the draft's
	want := []string{"flow", "compile 10.0.0.2.alpha inferred_mission flow vehicle dsl", "failsafe 10.0.0.2.alpha"}
	if record.Status != "deployed" || record.Failsafe != "mission" || strings.Join(d.calls, "|") != strings.Join(want, "|") {
		t.Fatalf("got %+v after %q", record, d.calls)
	}

	// The failsafe isn't set to a mission the vehicle didn't load
	d = &fakeDeployer{compileErr: errors.New("compile failed: FailedPrecondition - cannot load a mission while one is running")}
	s.SetDeployer(d)
	record = s.deploy(meta, meta.Plan, mission.Draft{DSL: "dsl", Status: "compiled"})
	if record.Status != "failed" || !strings.Contains(record.Error, "running") || len(d.calls) != 1 {
		t.Fatalf("got %+v after %q", record, d.calls)
	}
}

// The live directory may not exist yet when the first section's prompt is
// written
func TestWriteLivePromptImageMakesTheDirectory(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "verifier_live")
	if err := writeLivePromptImage(dir, []byte("jpeg")); err != nil {
		t.Fatal(err)
	}
	if data, _ := os.ReadFile(filepath.Join(dir, "prompt.jpg")); string(data) != "jpeg" {
		t.Fatalf("prompt.jpg is %q", data)
	}
}
