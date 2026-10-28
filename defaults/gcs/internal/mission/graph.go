// Package mission holds the GCS's mission-graph model and the checks run on
// graphs and uploaded mission binaries.
package mission

import (
	"errors"
	"fmt"
	"go/token"
	"regexp"
)

// GraphNode is an action instance in a mission graph.
type GraphNode struct {
	InstanceID string         `json:"instance_id"`
	TypeName   string         `json:"type_name"`
	Params     map[string]any `json:"params"`
}

// GraphEvent is an event instance in a mission graph; it has the same shape
// as a GraphNode.
type GraphEvent GraphNode

// GraphEdge is a transition from a node to another when an event fires.
type GraphEdge struct {
	Source  string `json:"source"`
	EventID string `json:"event_id"`
	Target  string `json:"target"`
}

// Graph is a mission as drawn on the Plan page.
type Graph struct {
	Nodes   []GraphNode  `json:"nodes"`
	Events  []GraphEvent `json:"events"`
	Edges   []GraphEdge  `json:"edges"`
	StartID *string      `json:"start_id"`
	// GeoJSON is the Map tab's drawn features; "" means none.
	GeoJSON string `json:"geojson"`
}

// GraphError is a problem in the frontend's {node_id, event_id, message}
// shape. Nil IDs encode as JSON null.
type GraphError struct {
	NodeID  *string `json:"node_id"`
	EventID *string `json:"event_id"`
	Message string  `json:"message"`
}

// DeclError is a compiler error attached to the declaration it was found in.
// Decl is empty when the error is not about a particular declaration.
type DeclError struct{ Decl, Message string }

const nameRule = "can only contain letters, digits and underscores, and can't start with a digit"

func ptr(s string) *string { return &s }

// StructuralErrors reports graph problems caught before compiling: duplicate
// or invalid ids, a missing or dangling start state, and dangling edges.
func StructuralErrors(g Graph) []GraphError {
	var errs []GraphError
	nodeIDs := map[string]bool{}
	for _, n := range g.Nodes {
		if nodeIDs[n.InstanceID] {
			errs = append(errs, GraphError{
				NodeID:  ptr(n.InstanceID),
				Message: fmt.Sprintf("Duplicate node instance_id: '%s'", n.InstanceID),
			})
		}
		nodeIDs[n.InstanceID] = true
		if !token.IsIdentifier(n.InstanceID) {
			errs = append(errs, GraphError{
				NodeID:  ptr(n.InstanceID),
				Message: fmt.Sprintf("Node name '%s' %s", n.InstanceID, nameRule),
			})
		}
	}
	eventIDs := map[string]bool{}
	for _, e := range g.Events {
		if eventIDs[e.InstanceID] {
			errs = append(errs, GraphError{
				EventID: ptr(e.InstanceID),
				Message: fmt.Sprintf("Duplicate event instance_id: '%s'", e.InstanceID),
			})
		}
		eventIDs[e.InstanceID] = true
		if e.InstanceID == "done" {
			errs = append(errs, GraphError{
				EventID: ptr("done"),
				Message: "Event name 'done' is reserved for the action-finished transition",
			})
		} else if !token.IsIdentifier(e.InstanceID) {
			errs = append(errs, GraphError{
				EventID: ptr(e.InstanceID),
				Message: fmt.Sprintf("Event name '%s' %s", e.InstanceID, nameRule),
			})
		}
	}

	if g.StartID == nil {
		errs = append(errs, GraphError{Message: "No start state: right-click a node and set it as the start state"})
	} else if !nodeIDs[*g.StartID] {
		errs = append(errs, GraphError{
			NodeID:  ptr(*g.StartID),
			Message: fmt.Sprintf("start_id '%s' does not refer to any node", *g.StartID),
		})
	}

	eventIDs["done"] = true
	for _, e := range g.Edges {
		if !nodeIDs[e.Source] {
			errs = append(errs, GraphError{
				NodeID:  ptr(e.Source),
				Message: fmt.Sprintf("Edge source '%s' does not refer to any node", e.Source),
			})
		}
		if !nodeIDs[e.Target] {
			errs = append(errs, GraphError{
				NodeID:  ptr(e.Target),
				Message: fmt.Sprintf("Edge target '%s' does not refer to any node", e.Target),
			})
		}
		if !eventIDs[e.EventID] {
			errs = append(errs, GraphError{
				EventID: ptr(e.EventID),
				Message: fmt.Sprintf("Edge event_id '%s' does not refer to any declared event", e.EventID),
			})
		}
	}
	return errs
}

// MapErrors converts compiler errors to the frontend's shape: an error on a
// decl names the node or event with that instance_id.
func MapErrors(errs []DeclError, g Graph) []GraphError {
	nodeIDs := map[string]bool{}
	for _, n := range g.Nodes {
		nodeIDs[n.InstanceID] = true
	}
	eventIDs := map[string]bool{}
	for _, e := range g.Events {
		eventIDs[e.InstanceID] = true
	}
	out := make([]GraphError, 0, len(errs))
	for _, e := range errs {
		ge := GraphError{Message: e.Message}
		if nodeIDs[e.Decl] {
			ge.NodeID = ptr(e.Decl)
		}
		if eventIDs[e.Decl] {
			ge.EventID = ptr(e.Decl)
		}
		out = append(out, ge)
	}
	return out
}

var nameRe = regexp.MustCompile(`^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$`)

// ValidName checks a mission name is safe to use as a file and registry name.
func ValidName(name string) error {
	if !nameRe.MatchString(name) {
		return errors.New("mission name must be 1-64 letters, digits, '_', '-' or '.', not starting with '.'")
	}
	return nil
}
