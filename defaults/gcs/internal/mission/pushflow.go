package mission

import (
	"context"
	"fmt"
	"net/http"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/flows"
	"github.com/rs/zerolog"
	"google.golang.org/grpc/status"
)

// FlowSetter sets a vehicle's flow; *fleet.Flows is one.
type FlowSetter interface {
	SetFlow(ctx context.Context, vehicle string, toml []byte) error
}

// FlowPush serves the Flow Builder's push of its flow to its vehicle, to
// test the flow without deploying a mission.
type FlowPush struct {
	list  Lister
	set   FlowSetter
	gcsIP string
	log   zerolog.Logger
}

// NewFlowPush returns a FlowPush that resolves flows with list, as Deploy
// does with the orchestrator at gcsIP, and sets them with set.
func NewFlowPush(list Lister, set FlowSetter, gcsIP string, log zerolog.Logger) *FlowPush {
	return &FlowPush{list: list, set: set, gcsIP: gcsIP, log: log}
}

type pushFlowRequest struct {
	Flow *flows.Template `json:"flow"`
}

// ServePushFlow serves POST /api/flows/push: it resolves the request's flow
// for the vehicle it was designed for, and sets it as that vehicle's flow.
func (p *FlowPush) ServePushFlow(w http.ResponseWriter, r *http.Request) {
	var req pushFlowRequest
	if !decodeBody(w, r, &req) {
		return
	}
	if req.Flow.Empty() {
		writeError(w, http.StatusBadRequest, "The flow is empty")
		return
	}
	name := req.Flow.Vehicle
	if name == "" {
		writeError(w, http.StatusBadRequest, "Select the flow's vehicle first")
		return
	}
	vs, cat, err := p.list(r.Context())
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "Cannot list vehicles: "+err.Error())
		return
	}
	i := -1
	for j := range vs {
		if vs[j].Name == name {
			i = j
		}
	}
	if i < 0 {
		writeError(w, http.StatusNotFound, fmt.Sprintf("Vehicle %s is not running", name))
		return
	}
	v := vs[i]
	if v.DriverErr != nil {
		writeError(w, http.StatusUnprocessableEntity, "Driver config: "+v.DriverErr.Error())
		return
	}
	// No mission reads the manifest, so it has no name or roles
	res, err := req.Flow.Resolve(cat, p.gcsIP, v, cameraNames(v), "", nil)
	if err != nil {
		writeError(w, http.StatusUnprocessableEntity, err.Error())
		return
	}
	if err := p.set.SetFlow(r.Context(), name, res.Flow); err != nil {
		writeError(w, http.StatusBadGateway, "Cannot set the vehicle's flow: "+status.Convert(err).Message())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"vehicle": name})
}
