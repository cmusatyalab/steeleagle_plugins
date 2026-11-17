package inferredmission

import (
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"regexp"
	"slices"
	"strconv"
	"strings"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
)

//go:embed prompts/v6.md
var visionPrompt string

const promptVersion = "v6"

const (
	targets       = "person|bicycle|car|motorcycle|bus|truck|train|boat|airplane|bird|dog|cat|horse|sheep|cow|elephant|bear|zebra|giraffe|bench|umbrella|backpack|suitcase|kite|frisbee|skateboard|surfboard|skis|snowboard"
	positive      = "[1-9][0-9]{0,4}"
	returnHome    = ", then return home\\."
	batteryReturn = " If the battery drops to 30 percent, return home\\."
	climbAction   = "gcsactions.ClimbToAltitude"
)

var takeoffTemplate = regexp.MustCompile("^Take off to (" + positive + ") meters, then (.+)$")

type taskTemplate struct {
	kind string
	re   *regexp.Regexp
}

var templates = []taskTemplate{
	{"track", regexp.MustCompile("^Track the (?P<target>" + targets + ")" + returnHome + batteryReturn + "$")},
	{"patrol", regexp.MustCompile("^Patrol the edge of the area at (?P<alt>" + positive + ") meters" + returnHome + batteryReturn + "$")},
	{"survey", regexp.MustCompile("^Survey the area in a lawnmower pattern at (?P<alt>" + positive + ") meters" + returnHome + batteryReturn + "$")},
	{"climb", regexp.MustCompile("^Climb to (?P<alt>" + positive + ") meters, then wait (?P<seconds>" + positive + ") seconds" + returnHome + "$")},
	{"wait", regexp.MustCompile("^Wait (?P<seconds>" + positive + ") seconds" + returnHome + "$")},
	{"precision_land", regexp.MustCompile("^Precision land on the (?P<target>" + targets + ")\\. If the battery drops to 30 percent, descend and land at the current position\\.$")},
	{"land", regexp.MustCompile("^Descend and land at the current position\\.$")},
}

type task struct {
	Kind     string `json:"task"`
	Target   string `json:"target,omitempty"`
	Altitude int    `json:"alt,omitempty"`
	Seconds  int    `json:"seconds,omitempty"`
	Takeoff  int    `json:"takeoff,omitempty"`
	Area     string `json:"area,omitempty"`
	Camera   string `json:"camera,omitempty"`
	Engine   string `json:"engine,omitempty"`
}

func parseInstruction(text string) (task, string, error) {
	instruction := strings.Join(strings.Fields(text), " ")
	body := instruction
	var takeoff int
	if match := takeoffTemplate.FindStringSubmatch(body); match != nil {
		takeoff, _ = strconv.Atoi(match[1])
		body = strings.ToUpper(match[2][:1]) + match[2][1:]
	}
	for _, template := range templates {
		match := template.re.FindStringSubmatch(body)
		if match == nil || (takeoff != 0 && (template.kind == "land" || template.kind == "precision_land")) {
			continue
		}
		value := func(name string) string {
			if i := template.re.SubexpIndex(name); i > 0 {
				return match[i]
			}
			return ""
		}
		altitude, _ := strconv.Atoi(value("alt"))
		seconds, _ := strconv.Atoi(value("seconds"))
		if takeoff%5 != 0 || altitude%5 != 0 || seconds%5 != 0 {
			return task{}, "", errors.New("V5 altitude and duration fields must be multiples of five.")
		}
		return task{Kind: template.kind, Target: value("target"), Altitude: altitude, Seconds: seconds, Takeoff: takeoff}, instruction, nil
	}
	return task{}, "", errors.New("The model reply does not match a v5 mission template.")
}

type historyItem struct {
	Section int     `json:"section"`
	Start   float64 `json:"start"`
	End     float64 `json:"end"`
	Weight  float64 `json:"weight"`
	Text    string  `json:"text"`
}

func selectHistory(rows []section, start float64, settings Settings) []historyItem {
	if settings.HistoryMode == "no_memory" || settings.HistorySections == 0 {
		return nil
	}
	var history []historyItem
	for _, row := range rows {
		if row.End > start || row.InferenceStatus != "valid" {
			continue
		}
		weight := 1.0
		if settings.HistoryMode == "recency" {
			age := math.Max(0, start-row.End) / settings.SectionSeconds
			weight = math.Round(math.Exp2(-age/settings.HalfLifeSections)*1e8) / 1e8
		}
		history = append(history, historyItem{Section: row.Index, Start: row.Start, End: row.End, Weight: weight, Text: row.InferenceText})
	}
	return history[max(0, len(history)-settings.HistorySections):]
}

func buildPrompt(base string, row section, mode string) string {
	data, _ := json.Marshal(map[string]any{
		"section": row.Index, "duration_seconds": row.End - row.Start, "history_mode": mode,
		"history": row.History, "telemetry": row.Telemetry,
	})
	return strings.TrimSpace(base) + "\n\n## Runtime context\n" +
		"Use the actual section duration and attached frame times. Only these images are current evidence. " +
		"Infer the task at the end of this section. History contains fallible hypotheses with relative priorities. " +
		"Clear visual evidence takes priority. Battery and return-home phrases are conventions, not observations. " +
		"Do not repeat an earlier takeoff.\n" + string(data)
}

func (t task) actionChain() [][]string {
	chain := map[string][][]string{
		"track":          {{"actions.Track"}, {"actions.ReturnToHome"}},
		"patrol":         {{"actions.Patrol"}, {"actions.ReturnToHome"}},
		"survey":         {{"actions.Patrol"}, {"actions.ReturnToHome"}},
		"climb":          {{climbAction}, {"actions.Wait"}, {"actions.ReturnToHome"}},
		"wait":           {{"actions.Wait"}, {"actions.ReturnToHome"}},
		"precision_land": {{"actions.PrecisionLand"}},
		"land":           {{"actions.Land"}},
	}[t.Kind]
	if t.Takeoff != 0 {
		chain = append([][]string{{"actions.TakeOff"}}, chain...)
	}
	return chain
}

func (t task) check(g mission.Graph, chain [][]string) []string {
	batteryAction := map[string]string{"track": "Track", "patrol": "Patrol", "survey": "Patrol", "precision_land": "PrecisionLand"}[t.Kind]
	wantActions := len(chain)
	if t.Kind == "precision_land" {
		wantActions++
	}
	if g.StartID == nil || len(g.Nodes) != wantActions {
		return []string{fmt.Sprintf("Expected exactly the inferred actions %v and any required battery fallback.", chain)}
	}
	if (batteryAction == "" && len(g.Events) != 0) || (batteryAction != "" && len(g.Events) != 1) {
		return []string{"The draft must have exactly the conditional event required by the inferred task."}
	}
	byID := make(map[string]mission.GraphNode, len(g.Nodes))
	events := make(map[string]mission.GraphEvent, len(g.Events))
	edges := make(map[string][]mission.GraphEdge)
	for _, node := range g.Nodes {
		byID[node.InstanceID] = node
	}
	for _, event := range g.Events {
		events[event.InstanceID] = event
	}
	for _, edge := range g.Edges {
		edges[edge.Source] = append(edges[edge.Source], edge)
	}
	id := *g.StartID
	for _, choices := range chain {
		node := byID[id]
		if !slices.Contains(choices, node.TypeName) {
			return []string{fmt.Sprintf("Expected %s on the normal completion path.", strings.Join(choices, " or "))}
		}
		kind := shortType(node.TypeName)
		if err := t.checkParams(kind, node.Params); err != "" {
			return []string{err}
		}
		next, conditional := "terminate", 0
		completions := 0
		for _, edge := range edges[id] {
			if edge.EventID == "done" {
				completions++
				next = edge.Target
				continue
			}
			conditional++
			event := events[edge.EventID]
			fallback := chain[len(chain)-1]
			if t.Kind == "precision_land" {
				fallback = []string{"Land", "actions.Land"}
			}
			comparator := shortType(field(event.Params, "Comparator"))
			if kind != batteryAction || (event.TypeName != "Battery" && event.TypeName != "events.Battery") ||
				!numericField(event.Params, "Threshold", 30) || comparator != "LessOrEqual" ||
				!slices.Contains(fallback, byID[edge.Target].TypeName) {
				return []string{"The only conditional transition must use the required low-battery event and fallback."}
			}
			if t.Kind == "precision_land" {
				for _, end := range edges[edge.Target] {
					if end.EventID != "done" || end.Target != "terminate" {
						return []string{"The battery fallback must terminate on completion."}
					}
				}
			}
		}
		if completions > 1 || (kind == batteryAction && conditional != 1) {
			return []string{"The action must have a single completion path and the required battery fallback."}
		}
		id = next
	}
	if id != "terminate" {
		return []string{"The final action must terminate on completion."}
	}
	return nil
}

func (t task) checkParams(kind string, params map[string]any) string {
	switch kind {
	case "TakeOff":
		if !numericField(params, "Altitude", t.Takeoff) {
			return "Takeoff altitude must match the inferred altitude."
		}
	case "Track":
		if field(params, "Class") != t.Target {
			return "Track class must match the inferred target."
		}
		if t.Camera != "" && shortType(field(params, "Camera")) != shortType(t.Camera) ||
			t.Engine != "" && field(params, "Engine") != t.Engine {
			return "Track must use the verified camera and detection engine."
		}
	case "Patrol":
		if !numericField(params, "Altitude", t.Altitude) {
			return "Patrol altitude must match the inferred altitude."
		}
		if shortType(field(params, "AltitudeMode")) != "AltitudeModeRelative" {
			return "Patrol altitude must use enums.AltitudeModeRelative for height above takeoff."
		}
		if t.Area != "" && field(params, "Area") != t.Area {
			return "Patrol area must match the selected map polygon."
		}
		pattern := shortType(field(params, "Pattern"))
		if (t.Kind == "survey" && pattern != "PatrolModeSurvey") ||
			(t.Kind == "patrol" && pattern != "" && pattern != "PatrolModeCorridor") {
			return "Use the inferred patrol or survey pattern."
		}
	case "ClimbToAltitude":
		if !numericField(params, "Altitude", t.Altitude) {
			return "Climb target height above takeoff must match the inference."
		}
	case "PrecisionLand":
		if field(params, "Class") != t.Target {
			return "Precision landing class must match the inference."
		}
	case "Wait":
		if !numericField(params, "Duration", t.Seconds) {
			return "Wait duration must match the inference."
		}
	}
	return ""
}

func shortType(s string) string {
	if i := strings.LastIndexByte(s, '.'); i != -1 {
		return s[i+1:]
	}
	return s
}

func field(params map[string]any, name string) string {
	for key, value := range params {
		if strings.EqualFold(key, name) {
			return fmt.Sprint(value)
		}
	}
	return ""
}

func numericField(params map[string]any, name string, want int) bool {
	value, err := strconv.ParseFloat(field(params, name), 64)
	return err == nil && value == float64(want)
}
