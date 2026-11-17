package inferredmission

import (
	"fmt"
	"go/token"
	"strings"
)

// draftDSL translates the finite set of validated V6 task shapes. Flight
// parameters and resource names come from the resolved task, not a second
// interpretation of the model's prose.
func draftDSL(t task) (string, error) {
	var actions, states []string
	add := func(action, state, fields string) {
		actions = append(actions, fmt.Sprintf("    %s %s(%s)\n", action, state, fields))
		states = append(states, state)
	}
	if t.Takeoff > 0 {
		add("actions.TakeOff", "takeoff", fmt.Sprintf("Altitude=%d.0", t.Takeoff))
	}
	batteryState := ""
	switch t.Kind {
	case "track":
		camera, ok := strings.CutPrefix(t.Camera, "params.Camera_")
		if !ok || !token.IsIdentifier("Camera_"+camera) || camera == "" || t.Engine == "" || t.Target == "" {
			return "", fmt.Errorf("tracking requires a verified camera, detection engine, and target class")
		}
		batteryState = "tracking"
		add("actions.Track", batteryState, fmt.Sprintf("Camera=%s, Engine=%q, Class=%q", t.Camera, t.Engine, t.Target))
	case "patrol", "survey":
		area := "Polygon_" + t.Area
		if t.Area == "" || !token.IsIdentifier(area) || t.Altitude <= 0 {
			return "", fmt.Errorf("patrol requires a selected map polygon and a positive height above takeoff")
		}
		fields := fmt.Sprintf("Area=params.%s, Altitude=%d.0, AltitudeMode=enums.AltitudeModeRelative", area, t.Altitude)
		if t.Kind == "survey" {
			fields += ", Pattern=actions.PatrolModeSurvey"
		}
		batteryState = "patrolling"
		add("actions.Patrol", batteryState, fields)
	case "climb":
		if t.Altitude <= 0 || t.Seconds <= 0 {
			return "", fmt.Errorf("climb requires a positive target height and wait duration")
		}
		add(climbAction, "climb", fmt.Sprintf("Altitude=%d.0", t.Altitude))
		add("actions.Wait", "waiting", fmt.Sprintf("Duration=%d.0", t.Seconds))
	case "wait":
		if t.Seconds <= 0 {
			return "", fmt.Errorf("wait requires a positive duration")
		}
		add("actions.Wait", "waiting", fmt.Sprintf("Duration=%d.0", t.Seconds))
	case "land":
		add("actions.Land", "landing", "")
	default:
		return "", fmt.Errorf("no V6 mission template for %q", t.Kind)
	}
	if t.Kind != "land" {
		add("actions.ReturnToHome", "home", "")
	}

	var dsl strings.Builder
	dsl.WriteString("Actions:\n")
	for _, action := range actions {
		dsl.WriteString(action)
	}
	if batteryState != "" {
		dsl.WriteString("Events:\n    events.Battery low_battery(Comparator=events.LessOrEqual, Threshold=30)\n")
	}
	fmt.Fprintf(&dsl, "Mission:\n    Start %s\n", states[0])
	for i, state := range states[:len(states)-1] {
		fmt.Fprintf(&dsl, "    During %s:\n        done -> %s\n", state, states[i+1])
		if state == batteryState {
			dsl.WriteString("        low_battery -> home\n")
		}
	}
	return dsl.String(), nil
}
