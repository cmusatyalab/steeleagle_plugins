package mission

import (
	"context"
	"fmt"
	"net/http"

	vehiclepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/vehicle"
	"google.golang.org/grpc/metadata"
)

// CompileOnVehicle sends the mission source dsl to the vehicle, which
// compiles it with its own eaglec against flow and geojson (either may be
// nil), saves it under name and loads it. It fails while a mission is
// running on the vehicle. ctx bounds the whole call, compile included.
func (s *Sender) CompileOnVehicle(ctx context.Context, vehicle, name string, flow, geojson []byte, dsl string) error {
	ctx = metadata.AppendToOutgoingContext(ctx, routeHeader, vehicle)
	_, err := vehiclepb.NewLoaderServiceClient(s.conn).CompileMission(ctx, vehiclepb.CompileMissionRequest_builder{
		Name: name, Flow: flow, Geojson: geojson, Mission: []byte(dsl),
	}.Build())
	if err != nil {
		return fmt.Errorf("compile failed: %s", describe(err))
	}
	return nil
}

// SetDataFailsafeMission makes the vehicle start its loaded mission when it
// loses contact with its RCS (the NO_DATA failsafe).
func (s *Sender) SetDataFailsafeMission(ctx context.Context, vehicle string) error {
	ctx = metadata.AppendToOutgoingContext(ctx, routeHeader, vehicle)
	ctx, cancel := context.WithTimeout(ctx, s.callTimeout)
	defer cancel()
	_, err := vehiclepb.NewSafetyServiceClient(s.conn).SetDataFailsafeBehavior(ctx, vehiclepb.SetDataFailsafeBehaviorRequest_builder{
		Behavior: vehiclepb.FailsafeBehavior_FAILSAFE_BEHAVIOR_MISSION,
	}.Build())
	if err != nil {
		return fmt.Errorf("setting the data failsafe failed: %s", describe(err))
	}
	return nil
}

// VehicleFlow is the flow TOML a mission graph g planned with plan runs with
// on its vehicle, names relative to the vehicle as Deploy bundles it; nil
// when plan has no flow.
func (s *Service) VehicleFlow(ctx context.Context, plan PlanningContext, g Graph) ([]byte, error) {
	target, _, err := s.planningTarget(ctx, plan)
	if err != nil {
		return nil, err
	}
	target, _, status, err := s.resolvePlanningFlow(ctx, plan, g, target)
	if err != nil {
		if status == http.StatusServiceUnavailable {
			return nil, fmt.Errorf("the flow can't be resolved now: %w", err)
		}
		return nil, err
	}
	return target.Flow, nil
}
