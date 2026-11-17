package inferredmission

import (
	"context"
	"encoding/json"
	"fmt"
	"maps"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/nl2dsl"
)

const fallbackWait = 30

var v6Templates = []taskTemplate{
	{"track", regexp.MustCompile(`^Track the (?P<target>[^\r\n]{1,128}?)` + returnHome + batteryReturn + `$`)},
	{"patrol", regexp.MustCompile(`^Patrol the edge of (?P<area>[^\r\n]{1,128}?) at (?P<alt>` + positive + `) meters` + returnHome + batteryReturn + `$`)},
	{"survey", regexp.MustCompile(`^Survey (?P<area>[^\r\n]{1,128}?) in a lawnmower pattern at (?P<alt>` + positive + `) meters` + returnHome + batteryReturn + `$`)},
	{"climb", regexp.MustCompile(`^Climb to (?P<alt>` + positive + `) meters, then wait ` + positive + ` seconds` + returnHome + `$`)},
	{"wait", regexp.MustCompile(`^Wait ` + positive + ` seconds` + returnHome + `$`)},
	{"land", regexp.MustCompile(`^Descend and land at the current position\.$`)},
}

func parseV6Instruction(text string) (task, error) {
	text = strings.TrimSpace(text)
	for _, template := range v6Templates {
		match := template.re.FindStringSubmatch(text)
		if match == nil {
			continue
		}
		value := func(name string) string {
			if i := template.re.SubexpIndex(name); i > 0 {
				return match[i]
			}
			return ""
		}
		t := task{Kind: template.kind}
		t.Altitude, _ = strconv.Atoi(value("alt"))
		if t.Kind == "climb" || t.Kind == "wait" {
			t.Seconds = fallbackWait
		}
		for name, dst := range map[string]*string{"target": &t.Target, "area": &t.Area} {
			raw := value(name)
			if raw == "" {
				continue
			}
			if strings.HasPrefix(raw, `"`) {
				if err := json.Unmarshal([]byte(raw), &raw); err != nil {
					return task{}, fmt.Errorf("the recommendation has an invalid quoted %s", name)
				}
			}
			if raw == "" || strings.TrimSpace(raw) != raw || strings.ContainsFunc(raw, unicode.IsControl) {
				return task{}, fmt.Errorf("the recommendation has an invalid %s name", name)
			}
			*dst = raw
		}
		return t, nil
	}
	return task{}, fmt.Errorf("the model did not return a supported V6 mission description")
}

func (t task) instructionV6() string {
	const battery = " If the battery drops to 30 percent, return home."
	switch t.Kind {
	case "track":
		return fmt.Sprintf("Track the %s, then return home.%s", t.Target, battery)
	case "patrol":
		return fmt.Sprintf("Patrol the edge of %s at %d meters, then return home.%s", t.Area, t.Altitude, battery)
	case "survey":
		return fmt.Sprintf("Survey %s in a lawnmower pattern at %d meters, then return home.%s", t.Area, t.Altitude, battery)
	case "climb":
		return fmt.Sprintf("Climb to %d meters, then wait %d seconds, then return home.", t.Altitude, t.Seconds)
	case "land":
		return "Descend and land at the current position."
	default:
		return fmt.Sprintf("Wait %d seconds, then return home.", t.Seconds)
	}
}

type v6TrackRoute struct {
	Camera     string    `json:"camera"`
	Engine     string    `json:"engine"`
	Detector   string    `json:"detector"`
	ClassNames *[]string `json:"class_names,omitempty"`
	cameraName string
}

type v6Capabilities struct {
	Actions    []string       `json:"actions"`
	Events     []string       `json:"events"`
	Track      []v6TrackRoute `json:"track_routes"`
	Polygons   []string       `json:"polygons"`
	FlowIssue  string         `json:"flow_issue,omitempty"`
	actionKeys map[string]string
}

func v6SchemaKeys(ref map[string]any, section string) map[string]string {
	schema, _ := ref["schema"].(map[string]any)
	entries, _ := schema[section].(map[string]any)
	keys := make(map[string]string, len(entries))
	for key, item := range entries {
		fields, _ := item.(map[string]any)
		name, _ := fields["type"].(string)
		if name == "" {
			name = key
		}
		keys[name] = key
	}
	return keys
}

func readV6Capabilities(ref map[string]any) v6Capabilities {
	actionKeys := v6SchemaKeys(ref, "actions")
	caps := v6Capabilities{
		Actions: slices.Sorted(maps.Keys(actionKeys)), Events: slices.Sorted(maps.Keys(v6SchemaKeys(ref, "events"))),
		Track: []v6TrackRoute{}, Polygons: []string{}, actionKeys: actionKeys,
	}
	// Read the public planning-reference shape without depending on the
	// compiler service's private camera and engine structs.
	var plan struct {
		FlowIssue   string            `json:"flow_issue"`
		MapFeatures map[string]string `json:"map_features"`
		Cameras     []struct {
			Name, Constant string
		}
		Engines []struct {
			Name        string
			FullName    string    `json:"full_name"`
			EngineType  string    `json:"engine_type"`
			CameraNames []string  `json:"camera_names"`
			ClassNames  *[]string `json:"class_names"`
		}
	}
	data, err := json.Marshal(ref["planning_context"])
	if err != nil || json.Unmarshal(data, &plan) != nil {
		return caps
	}
	caps.FlowIssue = plan.FlowIssue
	for _, name := range slices.Sorted(maps.Keys(plan.MapFeatures)) {
		if plan.MapFeatures[name] == "Polygon" {
			caps.Polygons = append(caps.Polygons, name)
		}
	}
	for _, engine := range plan.Engines {
		if engine.EngineType != "DetectionEngine" || engine.Name == "" {
			continue
		}
		for _, camera := range plan.Cameras {
			if camera.Constant != "" && slices.Contains(engine.CameraNames, camera.Name) {
				caps.Track = append(caps.Track, v6TrackRoute{
					Camera: camera.Constant, Engine: engine.Name, Detector: engine.FullName, ClassNames: engine.ClassNames, cameraName: camera.Name,
				})
			}
		}
	}
	return caps
}

func (c v6Capabilities) actionChain(t task) ([][]string, error) {
	chain := t.actionChain()
	for i, alternatives := range chain {
		var available []string
		for _, qualified := range alternatives {
			if key := c.actionKeys[qualified]; key != "" {
				available = append(available, key)
			}
		}
		if len(available) == 0 {
			return nil, fmt.Errorf("the selected vehicle's V4 registry does not provide %s", strings.Join(alternatives, " or "))
		}
		chain[i] = available
	}
	return chain, nil
}

func (c v6Capabilities) bind(t *task, vehicle string, evidence *liveEvidence) error {
	switch t.Kind {
	case "track", "patrol", "survey":
		if !slices.Contains(c.Events, "events.Battery") {
			return fmt.Errorf("the selected vehicle's V4 registry does not provide the required Battery event")
		}
	}
	switch t.Kind {
	case "track":
		if len(c.Track) == 0 {
			if c.FlowIssue != "" {
				return fmt.Errorf("the track route is unavailable: %s", c.FlowIssue)
			}
			return fmt.Errorf("Track requires a verified camera-to-detector route in Flow Builder")
		}
		var known, unknown []v6TrackRoute
		var observed *v6TrackRoute
		for _, route := range c.Track {
			if route.ClassNames != nil && !slices.Contains(*route.ClassNames, t.Target) {
				continue
			}
			if observedTrackClass(route, vehicle, evidence, t.Target) {
				observed = &route
			}
			if route.ClassNames == nil {
				unknown = append(unknown, route)
			} else {
				known = append(known, route)
			}
		}
		if observed != nil {
			t.Camera, t.Engine = observed.Camera, observed.Engine
			return nil
		}
		routes := known
		if len(routes) == 0 {
			routes = unknown
		}
		if len(routes) == 0 {
			return fmt.Errorf("no compatible detector advertises the class %q", t.Target)
		}
		if len(routes) != 1 {
			return fmt.Errorf("more than one camera-to-detector route could use the class %q", t.Target)
		}
		t.Camera, t.Engine = routes[0].Camera, routes[0].Engine
	case "patrol", "survey":
		if !slices.Contains(c.Polygons, t.Area) {
			return fmt.Errorf("no matching map polygon is defined for %q", t.Area)
		}
	}
	return nil
}

func observedTrackClass(route v6TrackRoute, vehicle string, evidence *liveEvidence, class string) bool {
	if evidence == nil || vehicle == "" || route.Detector == "" || route.Detector != evidence.Detector ||
		evidence.Producer != vehicle+"."+route.cameraName {
		return false
	}
	for _, frame := range evidence.Frames {
		if slices.ContainsFunc(frame.Detections, func(d detectorLabel) bool { return d.Class == class }) {
			return true
		}
	}
	return false
}

func buildV6Prompt(base string, row section, mode string, caps v6Capabilities) string {
	data, _ := json.Marshal(map[string]any{
		"section": row.Index, "duration_seconds": row.End - row.Start, "input_quality": row.InputQuality,
		"history_mode": mode, "history": row.History, "telemetry": row.Telemetry, "live_evidence": row.Evidence,
		"runtime_capabilities": caps,
	})
	return strings.TrimSpace(base) + "\n\n## Current evidence and runtime capabilities\n" + string(data)
}

func (s *Service) processV6(r *run, meta session, row section, images []nl2dsl.Image, frameErr error, start time.Time, publishVision func()) error {
	refCtx, cancel := context.WithTimeout(s.ctx, min(10*time.Second, timeout(meta.Settings.GenerationTimeout)))
	ref, refErr := s.generator.Compiler.DraftReference(refCtx, meta.Plan)
	cancel()
	caps := readV6Capabilities(ref)
	row.DSLReference = ref
	row.VisionPrompt = buildV6Prompt(r.prompt, row, meta.Settings.HistoryMode, caps)
	row.Status, row.InferenceStatus = "inferring", "running"
	if err := r.setSection(row); err != nil {
		return err
	}

	recommendation := task{Kind: "wait", Seconds: fallbackWait}
	var fallbackReason string
	switch {
	case frameErr != nil:
		fallbackReason = "Could not sample the section imagery: " + frameErr.Error()
	case len(images) == 0:
		fallbackReason = "No images were available for this section."
	case refErr != nil:
		fallbackReason = "The selected vehicle's mission capabilities could not be confirmed."
	default:
		inferenceStart := time.Now()
		ctx, cancel := context.WithTimeout(s.ctx, timeout(meta.Settings.RequestTimeout))
		reply, err := s.vision.Complete(ctx, nl2dsl.Request{Instructions: row.VisionPrompt, Images: images})
		cancel()
		row.VisionRequestTime = time.Since(inferenceStart).Seconds()
		row.VisionReply = reply.Text
		if err != nil {
			fallbackReason = "Vision was unavailable: " + err.Error()
		} else if proposed, err := parseV6Instruction(reply.Text); err != nil {
			fallbackReason = "Vision gave no usable mission recommendation: " + err.Error()
		} else if proposed.Kind == "wait" {
			fallbackReason = "The evidence did not establish a more specific continuation."
		} else if err := caps.bind(&proposed, meta.Plan.Vehicle, row.Evidence); err != nil {
			fallbackReason = "The proposed mission could not use the selected resources: " + err.Error()
		} else {
			recommendation = proposed
		}
	}
	if s.ctx.Err() != nil {
		row.Status, row.Error = "interrupted", "GCS stopped during analysis."
		return r.setSection(row)
	}
	row.Status, row.InferenceStatus = "generating", "valid"
	row.InferenceTime = time.Since(start).Seconds()
	setRecommendation := func() {
		current := recommendation
		row.Task, row.Instruction = &current, current.instructionV6()
		row.InferenceText = row.Instruction
		if fallbackReason != "" {
			row.InferenceStatus = "fallback"
			row.Adaptation = fallbackReason + " The 30-second wait comes from the fallback policy."
		} else if recommendation.Kind == "climb" {
			row.Adaptation = "The 30-second wait after the climb comes from the fallback policy."
		}
	}
	setRecommendation()
	if err := r.setSection(row); err != nil {
		return err
	}
	publishVision()

	generationStart := time.Now()
	build := func(t task, plan mission.PlanningContext) mission.Draft {
		ctx, cancel := context.WithTimeout(s.ctx, timeout(meta.Settings.GenerationTimeout))
		defer cancel()
		return s.compileV6(ctx, t, plan, caps)
	}
	var drafted mission.Draft
	if fallbackReason == "" {
		drafted = build(recommendation, meta.Plan)
		row.Attempts = append(slices.Clone(row.Attempts), drafted)
		if drafted.Status != "compiled" && s.ctx.Err() == nil {
			fallbackReason = "The proposed mission could not be compiled: " + strings.Join(drafted.Errors, " ")
		}
	}
	if fallbackReason != "" && s.ctx.Err() == nil {
		recommendation = task{Kind: "wait", Seconds: fallbackWait}
		setRecommendation()
		// Wait and ReturnToHome only need the selected driver. They should not
		// be blocked by an unrelated broken detector flow or drawing.
		drafted = build(recommendation, mission.PlanningContext{Vehicle: meta.Plan.Vehicle})
		row.Attempts = append(slices.Clone(row.Attempts), drafted)
	}
	row.GenerationTime = time.Since(generationStart).Seconds()
	row.CompileStatus = drafted.Status
	row.Status, row.Elapsed = "completed", time.Since(start).Seconds()
	switch {
	case s.ctx.Err() != nil:
		row.Status, row.Error = "interrupted", "GCS stopped during analysis."
	case drafted.Status == "compiled":
		row.DSL = drafted.DSL
		if drafted.Graph != nil {
			row.Mission = &missionArtifact{SchemaVersion: 1, Graph: *drafted.Graph}
		}
	default:
		row.Status = "failed"
		row.Error = strings.Join(drafted.Errors, " ")
	}
	return r.setSection(row)
}

func (s *Service) compileV6(ctx context.Context, t task, plan mission.PlanningContext, caps v6Capabilities) mission.Draft {
	dsl, err := draftDSL(t)
	if err != nil {
		return mission.Draft{Status: "invalid_task", Errors: []string{err.Error()}}
	}
	if len(caps.actionKeys) == 0 {
		ref, err := s.generator.Compiler.DraftReference(ctx, plan)
		if err != nil {
			return mission.Draft{Status: "compiler_unavailable", Errors: []string{fmt.Sprintf("Could not read the selected vehicle's mission schema: %v", err)}}
		}
		caps = readV6Capabilities(ref)
	}
	chain, err := caps.actionChain(t)
	if err != nil {
		return mission.Draft{Status: "unsupported_task", Errors: []string{err.Error()}}
	}
	return s.generator.Compiler.CheckDraft(ctx, dsl, plan, func(g mission.Graph) []string {
		return t.check(g, chain)
	})
}
