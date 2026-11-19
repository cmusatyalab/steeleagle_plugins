package inferredmission

import (
	"context"
	"errors"
	"testing"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/flows"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
	"github.com/rs/zerolog"
)

func TestPlanningFlowReplacesTheRunsFlow(t *testing.T) {
	s := &Service{ctx: context.Background(), log: zerolog.Nop()}
	builder := &flows.Template{Vehicle: "100.64.0.2.charlie", Engines: []flows.TemplateEngine{{Name: "stale.cliped"}}}
	plan := mission.PlanningContext{Vehicle: "100.64.0.2.charlie", Flow: builder}
	if got := s.planningContext(plan); got.Flow != builder {
		t.Error("the run's flow was replaced without a planning flow")
	}

	var asked string
	s.SetPlanningFlow(func(_ context.Context, vehicle string) (flows.Template, bool, error) {
		asked = vehicle
		return flows.Template{Vehicle: vehicle, Engines: []flows.TemplateEngine{{Name: "swap03.box_rerank"}}}, true, nil
	})
	got := s.planningContext(plan)
	if asked != "100.64.0.2.charlie" || got.Flow == nil || got.Flow.Engines[0].Name != "swap03.box_rerank" {
		t.Errorf("asked for %q, planned with %+v", asked, got.Flow)
	}
	if plan.Flow != builder {
		t.Error("the run's own plan was changed")
	}
	// Not for this vehicle: the run's own flow
	s.SetPlanningFlow(func(context.Context, string) (flows.Template, bool, error) { return flows.Template{}, false, nil })
	if got := s.planningContext(plan); got.Flow != builder {
		t.Error("the run's flow was replaced for another vehicle's default flow")
	}
	// Unusable: no flow, and why
	s.SetPlanningFlow(func(context.Context, string) (flows.Template, bool, error) {
		return flows.Template{}, true, errors.New("remote daemon unavailable")
	})
	if got := s.planningContext(plan); got.Flow != nil || got.FlowIssue == "" {
		t.Errorf("an unusable default flow: %+v", got)
	}
}
