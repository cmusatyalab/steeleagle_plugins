package gcsactions

import (
	"context"
	"math"
	"net"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	commonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/common"
	telemetrypb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/telemetry"
	driverpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/driver"
	vehiclepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/vehicle"
	"github.com/cmusatyalab/steeleagle/sdk"
	"github.com/cmusatyalab/steeleagle/sdk/dsl"
	"github.com/rs/zerolog"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type climbDriver struct {
	driverpb.UnimplementedControlServiceServer
	vehiclepb.UnimplementedDataServiceServer
	initial, target float32
	stale           bool
	command         atomic.Pointer[driverpb.SetRelativePositionTargetRequest]
	checks          atomic.Uint32
}

func (d *climbDriver) SetRelativePositionTarget(_ context.Context, req *driverpb.SetRelativePositionTargetRequest) (*driverpb.SetRelativePositionTargetResponse, error) {
	d.command.Store(req)
	return &driverpb.SetRelativePositionTargetResponse{}, nil
}

func (d *climbDriver) GetTelemetry(context.Context, *vehiclepb.GetTelemetryRequest) (*vehiclepb.GetTelemetryResponse, error) {
	altitude := d.initial
	motion := telemetrypb.MotionStatus_MOTION_STATUS_HOLDING
	if d.command.Load() != nil {
		altitude = d.target
		if d.checks.Add(1) == 1 {
			motion = telemetrypb.MotionStatus_MOTION_STATUS_IN_TRANSIT
		}
	}
	timestamp := time.Now()
	if d.stale {
		timestamp = timestamp.Add(-time.Minute)
	}
	relative := &commonpb.RelativePosition{}
	relative.SetZ(altitude)
	position := &telemetrypb.PositionInfo{}
	position.SetRelativePosition(relative)
	sample := &telemetrypb.Telemetry{}
	sample.SetTimestamp(timestamppb.New(timestamp))
	sample.SetPositionInfo(position)
	sample.SetMotionStatus(motion)
	return vehiclepb.GetTelemetryResponse_builder{Telemetry: []*telemetrypb.Telemetry{sample}}.Build(), nil
}

func climbTestVehicle(t *testing.T, driver *climbDriver) sdk.Vehicle {
	t.Helper()
	lis := bufconn.Listen(1 << 20)
	server := grpc.NewServer()
	driverpb.RegisterControlServiceServer(server, driver)
	vehiclepb.RegisterDataServiceServer(server, driver)
	go func() { _ = server.Serve(lis) }()
	t.Cleanup(server.Stop)
	conn, err := grpc.NewClient("passthrough:///vehicle", grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return lis.DialContext(ctx) }))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	ctx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
	t.Cleanup(cancel)
	return sdk.NewVehicleFromContext(ctx, conn, sdk.WithLogger(zerolog.Nop()))
}

func TestClimbUsesAbsoluteHeightAndWaitsForHold(t *testing.T) {
	driver := &climbDriver{initial: 3, target: 5}
	action := &ClimbToAltitude{Altitude: 5}
	if err := action.Execute(climbTestVehicle(t, driver), dsl.MissionData{}); err != nil {
		t.Fatal(err)
	}
	req := driver.command.Load()
	if req == nil {
		t.Fatal("no climb command was sent")
	}
	position := req.GetPosition()
	if position.GetX() != 0 || position.GetY() != 0 || position.GetZ() != 2 || position.GetAngle() != 0 ||
		req.GetFrame() != driverpb.ReferenceFrame_REFERENCE_FRAME_BODY {
		t.Errorf("climb command = %v, want 2 m upwards from 3 m to 5 m", req)
	}
	if driver.checks.Load() < 2 {
		t.Error("climb completed before telemetry confirmed a hold at 5 m")
	}
}

func TestClimbSkipsDescentAndRejectsBadTelemetry(t *testing.T) {
	for _, tc := range []struct {
		name    string
		initial float32
		stale   bool
		wantErr string
	}{
		{name: "above target", initial: 6},
		{name: "stale reading", initial: 3, stale: true, wantErr: "fresh telemetry"},
		{name: "invalid reading", initial: float32(math.NaN()), wantErr: "finite relative-altitude"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			driver := &climbDriver{initial: tc.initial, stale: tc.stale}
			err := (&ClimbToAltitude{Altitude: 5}).Execute(climbTestVehicle(t, driver), dsl.MissionData{})
			if tc.wantErr == "" && err != nil || tc.wantErr != "" && (err == nil || !strings.Contains(err.Error(), tc.wantErr)) {
				t.Errorf("Execute() = %v, want %q", err, tc.wantErr)
			}
			if driver.command.Load() != nil {
				t.Error("climb issued a position command without a usable altitude below the target")
			}
		})
	}
}
