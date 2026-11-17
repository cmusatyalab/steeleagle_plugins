package inferredmission

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/flows"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
)

func TestV6PreservesNativeClassFromTheSelectedDetector(t *testing.T) {
	got, err := parseV6Instruction(`Track the "Inspection Marker: Red", then return home. If the battery drops to 30 percent, return home.`)
	if err != nil || got.Target != "Inspection Marker: Red" {
		t.Fatalf("recommendation = %#v, %v", got, err)
	}
	caps := v6Capabilities{Events: []string{"events.Battery"}, Track: []v6TrackRoute{
		{Camera: "params.Camera_rgb", Engine: "test.det", Detector: "100.0.0.2.det", cameraName: "rgb"},
		{Camera: "params.Camera_other", Engine: "test.other", Detector: "100.0.0.2.other", cameraName: "other"},
	}}
	evidence := &liveEvidence{
		Producer: "100.0.0.2.drone.rgb",
		Detector: "100.0.0.2.det",
		Frames:   []frameEvidence{{Detections: []detectorLabel{{Class: got.Target}}}},
	}
	if err := caps.bind(&got, "100.0.0.2.drone", evidence); err != nil || got.Camera != "params.Camera_rgb" || got.Engine != "test.det" {
		t.Fatalf("binding = %#v, %v", got, err)
	}
	if err := caps.bind(&got, "100.0.0.2.different", evidence); err == nil {
		t.Fatal("another vehicle's detection selected an ambiguous track route")
	}
	dogs := []string{"Dog"}
	caps.Track = []v6TrackRoute{{Camera: "params.Camera_rgb", Engine: "test.det", ClassNames: &dogs}}
	if err := caps.bind(&got, "100.0.0.2.drone", evidence); err == nil {
		t.Fatal("admitted a class excluded by the selected detector")
	}
}

type fallbackTestCompiler struct {
	videoCompiler
	checkedPlan  mission.PlanningContext
	checkedDraft string
}

func (c *fallbackTestCompiler) CheckDraft(ctx context.Context, dsl string, plan mission.PlanningContext, check func(mission.Graph) []string) mission.Draft {
	c.checkedPlan, c.checkedDraft = plan, dsl
	return c.videoCompiler.CheckDraft(ctx, dsl, plan, check)
}

func TestV6MissingImageryCompilesPolicyFallbackWithTheSelectedDriver(t *testing.T) {
	s := testService(t, t.TempDir(), nil, nil)
	compiler := &fallbackTestCompiler{}
	s.generator.Compiler = compiler
	r := &run{dir: t.TempDir(), rows: map[int]section{}, changed: make(chan struct{}), prompt: visionPrompt}
	meta := session{Settings: s.settings, PromptVersion: promptVersion, Plan: mission.PlanningContext{
		Vehicle: "test.drone", Flow: &flows.Template{Vehicle: "unavailable"}, GeoJSON: "ignored",
	}}
	err := s.process(r, meta, section{sectionSummary: sectionSummary{Index: 1}}, func() ([]sample, error) {
		return nil, fmt.Errorf("fixture has no decodable frame")
	})
	if err != nil {
		t.Fatal(err)
	}
	got := r.rows[1]
	if got.InferenceText != "Wait 30 seconds, then return home." || got.CompileStatus != "compiled" ||
		got.Mission == nil || !strings.Contains(got.Adaptation, "30-second wait comes from the fallback policy") ||
		!strings.Contains(got.Adaptation, "no decodable frame") {
		t.Fatalf("fallback result = %#v", got)
	}
	if compiler.checkedPlan.Vehicle != "test.drone" || compiler.checkedPlan.Flow != nil || compiler.checkedPlan.GeoJSON != "" ||
		!strings.Contains(compiler.checkedDraft, "actions.Wait waiting(Duration=30.0)") ||
		!strings.Contains(compiler.checkedDraft, "done -> home") {
		t.Fatalf("compiler plan = %#v; DSL = %q", compiler.checkedPlan, compiler.checkedDraft)
	}
}
