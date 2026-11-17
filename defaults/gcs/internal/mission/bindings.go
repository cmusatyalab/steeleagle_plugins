package mission

import (
	"fmt"
	"slices"
	"strings"

	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/flows"
)

type cameraBinding struct {
	Name      string `json:"name"`
	Constant  string `json:"constant"`
	FrameType string `json:"frame_type"`
}

type engineBinding struct {
	Name                  string   `json:"name"`
	FullName              string   `json:"full_name"`
	Constant              string   `json:"constant"`
	EngineType            string   `json:"engine_type"`
	ResultType            string   `json:"result_type"`
	CameraNames           []string `json:"camera_names"`
	UnverifiedCameraNames []string `json:"unverified_camera_names,omitempty"`
}

type planningBindings struct {
	Cameras []cameraBinding
	Engines []engineBinding
}

func driverBindings(plugin []byte) planningBindings {
	bindings := planningBindings{Cameras: []cameraBinding{}, Engines: []engineBinding{}}
	if len(plugin) == 0 {
		return bindings
	}
	streams, _, err := flows.ParseDriver(plugin)
	if err != nil {
		return bindings
	}
	for _, stream := range streams {
		if stream.Kind == "camera" {
			bindings.Cameras = append(bindings.Cameras, cameraBinding{
				Name: stream.Name, Constant: "params.Camera_" + constName(stream.Name), FrameType: stream.Type,
			})
		}
	}
	return bindings
}

func (b planningBindings) forRegistry(ix *TypeIndex) planningBindings {
	// The SDK may omit constants from the scoped schema.
	values := fieldList(ix.enums["params.Camera"], false)
	cameras := make([]cameraBinding, 0, len(b.Cameras))
	for _, camera := range b.Cameras {
		if slices.ContainsFunc(values, func(v map[string]any) bool { return str(v, "name") == Bare(camera.Constant) }) {
			cameras = append(cameras, camera)
		}
	}
	b.Cameras = cameras
	return b
}

func (b *planningBindings) addFlow(catalog flows.Catalog, template flows.Template, resolved flows.Resolved) {
	flow, err := config.ParseFlowConfigFromBytes(resolved.Flow)
	if err != nil {
		return
	}
	engines := make(map[string]flows.Engine, len(resolved.Engines))
	for name, relative := range resolved.Engines {
		host, bare, _ := flows.SplitTemplateName(name)
		ip, _ := catalog.ResolveHost(host)
		engines[relative], _ = catalog.Engine(ip + "." + bare)
	}
	next := map[string][]string{}
	for _, edge := range flow.Edges {
		next[edge.From] = append(next[edge.From], edge.To)
	}
	routes := make(map[string]map[string]frameRoute, len(b.Cameras))
	for _, camera := range b.Cameras {
		// Use resolved vehicle producer names. An independent engine producer
		// advertises its type but does not establish the originating camera.
		routes[camera.Name] = traceCamera(fleet.VehicleStream(camera.Name), camera.FrameType, next, engines)
	}
	for _, entry := range template.Engines {
		relative := resolved.Engines[entry.Name]
		engine := engines[relative]
		engineType := resultEngineType(engine.Result)
		if engineType == "" {
			continue
		}
		binding := engineBinding{
			Name: entry.Name, FullName: engine.Name, Constant: "params." + engineType + "_" + constName(relative),
			EngineType: engineType, ResultType: engine.Result, CameraNames: []string{},
		}
		for _, camera := range b.Cameras {
			switch routes[camera.Name][relative] {
			case verifiedRoute:
				binding.CameraNames = append(binding.CameraNames, camera.Name)
			case unverifiedRoute:
				binding.UnverifiedCameraNames = append(binding.UnverifiedCameraNames, camera.Name)
			}
		}
		b.Engines = append(b.Engines, binding)
	}
}

// resultEngineType follows the SDK's manifest-to-params mapping. A sink
// without a result is part of the flow but has no mission engine constant.
func resultEngineType(result string) string {
	switch name := result[strings.LastIndex(result, "/")+1:]; name {
	case "":
		return ""
	case "DetectionResult", "ClassificationResult", "GuidanceResult", "PositionResult":
		return strings.TrimSuffix(name, "Result") + "Engine"
	default:
		return "GenericEngine"
	}
}

type frameRoute int

const (
	unverifiedRoute frameRoute = iota + 1
	verifiedRoute
)

// traceCamera walks output edges using the config v0.0.8 conversion rules.
// A result event and an advertised producer are separate from these edges.
func traceCamera(producer, frame string, next map[string][]string, engines map[string]flows.Engine) map[string]frameRoute {
	type step struct{ node, frame string }
	reached := map[string]frameRoute{}
	seen := map[step]bool{}
	queue := []step{{producer, frame}}
	for len(queue) > 0 {
		input := queue[0]
		queue = queue[1:]
		if seen[input] {
			continue
		}
		seen[input] = true
		for _, name := range next[input.node] {
			engine, ok := engines[name]
			if !ok {
				continue
			}
			accepts := func(frame string) bool { return slices.Contains(engine.Accepts, frame) }
			if engine.Typed && input.frame != "" && !accepts(input.frame) {
				continue
			}
			if !engine.Typed || input.frame == "" {
				reached[name] = max(reached[name], unverifiedRoute)
				queue = append(queue, step{node: name})
				continue
			}
			reached[name] = verifiedRoute
			switch forwards := engine.Forwards; {
			case forwards == "" || forwards == config.ForwardsAll:
				queue = append(queue, step{name, input.frame})
			case forwards == input.frame || !accepts(forwards):
				queue = append(queue, step{name, forwards})
			}
		}
	}
	return reached
}

func placeholder(value, goType string) bool {
	value = strings.TrimPrefix(Bare(value), Bare(goType)+"_")
	return strings.HasPrefix(strings.ToUpper(value), "UNBOUND_")
}

func cameraFor(value string, cameras []cameraBinding) (cameraBinding, bool) {
	for _, candidate := range cameras {
		if value == candidate.Constant || value == Bare(candidate.Constant) {
			return candidate, true
		}
	}
	return cameraBinding{}, false
}

func isTrack(ix *TypeIndex, node GraphNode) bool {
	qualified, _ := ix.Qualify("actions", node.TypeName)
	camera, _ := ix.FieldType(qualified, "Camera")
	engine, _ := ix.FieldType(qualified, "Engine")
	return qualified == "actions.Track" && camera == "params.Camera" && engine == "params.DetectionEngine"
}

func bindTrackDraft(g Graph, ix *TypeIndex, bindings planningBindings) {
	for _, node := range g.Nodes {
		if !isTrack(ix, node) {
			continue
		}
		camera, _ := node.Params["Camera"].(string)
		engine, _ := node.Params["Engine"].(string)
		needCamera := placeholder(camera, "params.Camera")
		needEngine := placeholder(engine, "params.DetectionEngine")
		if !needCamera && !needEngine {
			continue
		}
		var chosenCamera, chosenEngine string
		possible := 0
		for _, e := range bindings.Engines {
			if e.EngineType != "DetectionEngine" || (!needEngine && engine != e.Name) {
				continue
			}
			for _, c := range bindings.Cameras {
				if !needCamera && camera != Bare(c.Constant) && camera != c.Constant {
					continue
				}
				if slices.Contains(e.CameraNames, c.Name) {
					chosenCamera, chosenEngine = Bare(c.Constant), e.Name
					possible++
				} else if slices.Contains(e.UnverifiedCameraNames, c.Name) {
					possible++
				}
			}
		}
		if possible != 1 || chosenEngine == "" {
			continue
		}
		if needCamera {
			node.Params["Camera"] = chosenCamera
		}
		if needEngine {
			node.Params["Engine"] = chosenEngine
		}
	}
}

func draftBindingErrors(g Graph, ix *TypeIndex, plan PlanningContext, bindings planningBindings, flowErr error) []GraphError {
	issues := bindingErrors(g, ix, plan, bindings, true)
	if flowErr != nil {
		issues = append(issues, GraphError{Message: flowErr.Error()})
	} else if len(issues) > 0 && plan.Flow.Empty() && plan.FlowIssue != "" {
		issues = append(issues, GraphError{Message: plan.FlowIssue})
	}
	return issues
}

func bindingErrors(g Graph, ix *TypeIndex, plan PlanningContext, bindings planningBindings, draft bool) []GraphError {
	var issues []GraphError
	features := featureNames(plan.GeoJSON)
	engines := make(map[string]engineBinding, len(bindings.Engines))
	for _, engine := range bindings.Engines {
		engines[engine.Name] = engine
	}
	check := func(section string, node GraphNode) {
		qualified, _ := ix.Qualify(section, node.TypeName)
		add := func(field, message string) {
			issue := GraphError{Message: fmt.Sprintf("%s.%s: %s", node.InstanceID, field, message)}
			if section == "actions" {
				issue.NodeID = ptr(node.InstanceID)
			} else {
				issue.EventID = ptr(node.InstanceID)
			}
			issues = append(issues, issue)
		}
		for _, field := range orderedParams(ix, qualified, node.Params) {
			raw := node.Params[field]
			value, isString := raw.(string)
			goType, _ := ix.FieldType(qualified, field)
			unbound := placeholder(value, goType)
			wrongType := raw != nil && !isString
			switch {
			case isFeatureType(goType) && (wrongType || unbound || (draft && features[value] != Bare(goType))):
				add(field, fmt.Sprintf("Draw or select a %s in the Map tab for this field.", Bare(goType)))
			case isEngineType(goType):
				engine, found := engines[value]
				if wrongType || unbound || draft && !found {
					add(field, fmt.Sprintf("Connect a %s in Flow Builder and select it for this action.", Bare(goType)))
				} else if found && engine.EngineType != Bare(goType) {
					add(field, fmt.Sprintf("Select a %s. Flow engine %q produces %s results.", Bare(goType), value, engine.EngineType))
				}
			case goType == "params.Camera":
				_, found := cameraFor(value, bindings.Cameras)
				trackCamera := isTrack(ix, node)
				if wrongType {
					add(field, "Expected a camera identifier. Select a camera in Mission Builder.")
				} else if unbound || trackCamera && value == "" || !found && (plan.Vehicle != "" || draft && trackCamera) {
					add(field, "Select a vehicle and one of its cameras in Mission Builder.")
				}
			}
		}
		if section != "actions" || !isTrack(ix, node) {
			return
		}
		cameraName, _ := node.Params["Camera"].(string)
		engineName, _ := node.Params["Engine"].(string)
		camera, haveCamera := cameraFor(cameraName, bindings.Cameras)
		engine, haveEngine := engines[engineName]
		if haveCamera && haveEngine && engine.EngineType == "DetectionEngine" && !slices.Contains(engine.CameraNames, camera.Name) {
			if !slices.Contains(engine.UnverifiedCameraNames, camera.Name) {
				add("Engine", fmt.Sprintf("Connect camera %q to detector %q through compatible frame types in Flow Builder.", camera.Name, engineName))
			} else if draft {
				add("Engine", fmt.Sprintf("The frame route from camera %q to detector %q could not be verified.", camera.Name, engineName))
			}
		}
	}
	for _, node := range g.Nodes {
		check("actions", node)
	}
	for _, event := range g.Events {
		check("events", GraphNode(event))
	}
	return issues
}
