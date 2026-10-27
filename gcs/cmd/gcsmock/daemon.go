package main

import (
	"bytes"
	"context"
	"sync"

	telemetrypb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/telemetry"
	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	driverpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/driver"
	missionpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/mission"
	vehiclepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/vehicle"
	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"github.com/rs/zerolog"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"time"
)

const (
	routeHeader = "x-route-key"
	// takeOffDefault is the altitude of a TakeOff that gives none.
	takeOffDefault = 10
)

// FlowApplier runs a vehicle's dataflow. The flow it receives has producer
// names qualified and remote engines made local.
type FlowApplier interface {
	Apply(vehicle string, flow *config.FlowConfig) error
}

// Topology is the names the mock daemon lists.
type Topology struct {
	GCSIP string
	// Vehicles are the full vehicle names, "{daemon IP}.{name}".
	Vehicles []string
	// EncoderDaemons are the IPs of the daemons that have an h264 encoder.
	EncoderDaemons map[string]bool
}

// Daemon is the mock daemon: it lists the topology and serves the vehicle
// services of each vehicle by route key.
type Daemon struct {
	daemonpb.UnimplementedDaemonServiceServer
	vehiclepb.UnimplementedDataServiceServer
	driverpb.UnimplementedControlServiceServer
	missionpb.UnimplementedMissionServiceServer

	top   Topology
	flows FlowApplier
	log   zerolog.Logger
	state map[string]*vehicleState

	remoteOnce sync.Once
}

// vehicleState is a vehicle and its dataflow.
type vehicleState struct {
	v *Vehicle

	mu   sync.Mutex // held while the flow changes, including while it is applied
	spec []byte
}

// NewDaemon returns a daemon serving vehicles, keyed by full name.
func NewDaemon(top Topology, vehicles map[string]*Vehicle, flows FlowApplier, log zerolog.Logger) *Daemon {
	d := &Daemon{top: top, flows: flows, log: log, state: map[string]*vehicleState{}}
	for n, v := range vehicles {
		d.state[n] = &vehicleState{v: v}
	}
	return d
}

// Register registers the services on s.
func (d *Daemon) Register(s *grpc.Server) {
	daemonpb.RegisterDaemonServiceServer(s, d)
	vehiclepb.RegisterDataServiceServer(s, d)
	driverpb.RegisterControlServiceServer(s, d)
	missionpb.RegisterMissionServiceServer(s, d)
}

// route returns the vehicle the call's route key names. It mirrors the
// router: a missing or unknown key is NotFound.
func (d *Daemon) route(ctx context.Context) (*vehicleState, string, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	keys := md.Get(routeHeader)
	if len(keys) == 0 {
		return nil, "", status.Errorf(codes.NotFound, "no route for key %q", "")
	}
	st, ok := d.state[keys[0]]
	if !ok {
		return nil, "", status.Errorf(codes.NotFound, "no route for key %q", keys[0])
	}
	return st, keys[0], nil
}

// driverConfig is the plugin config of a mock vehicle's driver.
func driverConfig() ([]byte, error) {
	pc := config.PluginConfig{
		Plugin: config.PluginRoot{Name: "mock_vehicle", Type: "driver"},
		Driver: &config.DriverCap{
			Type: "copter",
			Cameras: []config.DriverCamera{{
				Name: "front", Type: "rgb", HRes: 1280, VRes: 720, HFov: 90, VFov: 60,
				Fps: 30, Primary: true, Encode: true,
			}},
			Supported: config.DriverSupported{Services: []string{
				"steeleagle_protocol/v1/services/driver/ControlService",
				"steeleagle_protocol/v1/services/driver/StreamService",
			}},
		},
	}
	return pc.ToBytes()
}

// ListServices lists the vehicles, the encoders and the GCS engine.
func (d *Daemon) ListServices(
	ctx context.Context, req *daemonpb.ListServicesRequest,
) (*daemonpb.ListServicesResponse, error) {
	conf, err := driverConfig()
	if err != nil {
		return nil, status.Error(codes.Internal, err.Error())
	}
	var out []*daemonpb.Service
	for _, n := range d.top.Vehicles {
		out = append(out, daemonpb.Service_builder{
			Name: n, Category: daemonpb.ServiceCategory_SERVICE_CATEGORY_VEHICLE,
			Plugin: "mock_vehicle", Config: conf, Running: true, Auth: "admin",
		}.Build())
	}
	seen := map[string]bool{}
	for _, n := range d.top.Vehicles {
		ip, _ := fleet.SplitName(n)
		if d.top.EncoderDaemons[ip] && !seen[ip] {
			seen[ip] = true
			out = append(out, daemonpb.Service_builder{
				Name: ip + "." + fleet.EncoderService, Category: daemonpb.ServiceCategory_SERVICE_CATEGORY_ENGINE,
				Plugin: fleet.EncoderService, Running: true, Auth: "admin",
			}.Build())
		}
	}
	out = append(out, daemonpb.Service_builder{
		Name: d.top.GCSIP + ".gcs", Category: daemonpb.ServiceCategory_SERVICE_CATEGORY_ENGINE,
		Plugin: "gcs", Running: true, Auth: "admin",
	}.Build())
	return daemonpb.ListServicesResponse_builder{Services: out}.Build(), nil
}

// GetTelemetry returns the vehicle's current telemetry.
func (d *Daemon) GetTelemetry(
	ctx context.Context, req *vehiclepb.GetTelemetryRequest,
) (*vehiclepb.GetTelemetryResponse, error) {
	st, _, err := d.route(ctx)
	if err != nil {
		return nil, err
	}
	return vehiclepb.GetTelemetryResponse_builder{
		Telemetry: []*telemetrypb.Telemetry{st.v.Telemetry(time.Now())},
	}.Build(), nil
}

// GetFlow returns the bytes the flow was set with.
func (d *Daemon) GetFlow(ctx context.Context, req *vehiclepb.GetFlowRequest) (*vehiclepb.GetFlowResponse, error) {
	st, _, err := d.route(ctx)
	if err != nil {
		return nil, err
	}
	st.mu.Lock()
	defer st.mu.Unlock()
	return vehiclepb.GetFlowResponse_builder{Toml: st.spec}.Build(), nil
}

// SetDataflow replaces the vehicle's flow. Setting the current bytes again
// does nothing.
func (d *Daemon) SetDataflow(
	ctx context.Context, req *vehiclepb.SetDataflowRequest,
) (*vehiclepb.SetDataflowResponse, error) {
	st, name, err := d.route(ctx)
	if err != nil {
		return nil, err
	}
	return &vehiclepb.SetDataflowResponse{}, d.setFlow(st, name, req.GetToml())
}

func (d *Daemon) setFlow(st *vehicleState, name string, spec []byte) error {
	st.mu.Lock()
	defer st.mu.Unlock()
	if st.spec != nil && bytes.Equal(st.spec, spec) {
		return nil
	}
	flow, err := config.ParseFlowConfigFromBytes(spec)
	if err != nil {
		return status.Errorf(codes.InvalidArgument, "invalid dataflow: %v", err)
	}
	qualified := d.qualify(name, flow)
	if err := d.flows.Apply(name, qualified); err != nil {
		return status.Error(codes.Internal, err.Error())
	}
	st.spec = bytes.Clone(spec)
	return nil
}

// qualify returns flow with producers named as the real vehicle does and
// remote engines made local, as the mock has one process.
func (d *Daemon) qualify(vehicle string, flow *config.FlowConfig) *config.FlowConfig {
	out := &config.FlowConfig{
		Producers: make([]config.Producer, len(flow.Producers)),
		Engines:   make([]config.Engine, len(flow.Engines)),
		Edges:     make([]config.Edge, len(flow.Edges)),
	}
	copy(out.Producers, flow.Producers)
	copy(out.Engines, flow.Engines)
	copy(out.Edges, flow.Edges)
	for i, p := range out.Producers {
		out.Producers[i].Name = vehicle + "." + p.Name
		for j, e := range out.Edges {
			if e.From == p.Name {
				out.Edges[j].From = out.Producers[i].Name
			}
		}
	}
	for i, e := range out.Engines {
		if e.Location == config.LocationRemote {
			d.remoteOnce.Do(func() {
				d.log.Info().Msg("rewriting remote engines to local: the mock runs everything in one process")
			})
			out.Engines[i].Location = config.LocationLocal
		}
	}
	return out
}

// TakeOff takes off.
func (d *Daemon) TakeOff(ctx context.Context, req *driverpb.TakeOffRequest) (*driverpb.TakeOffResponse, error) {
	st, _, err := d.route(ctx)
	if err != nil {
		return nil, err
	}
	alt := float64(takeOffDefault)
	if req.HasAltitude() {
		alt = float64(req.GetAltitude())
	}
	st.v.TakeOff(alt)
	return &driverpb.TakeOffResponse{}, nil
}

// Land lands.
func (d *Daemon) Land(ctx context.Context, req *driverpb.LandRequest) (*driverpb.LandResponse, error) {
	st, _, err := d.route(ctx)
	if err != nil {
		return nil, err
	}
	st.v.Land()
	return &driverpb.LandResponse{}, nil
}

// ReturnToHome returns to home.
func (d *Daemon) ReturnToHome(
	ctx context.Context, req *driverpb.ReturnToHomeRequest,
) (*driverpb.ReturnToHomeResponse, error) {
	st, _, err := d.route(ctx)
	if err != nil {
		return nil, err
	}
	st.v.ReturnToHome()
	return &driverpb.ReturnToHomeResponse{}, nil
}

// Hold holds.
func (d *Daemon) Hold(ctx context.Context, req *driverpb.HoldRequest) (*driverpb.HoldResponse, error) {
	st, _, err := d.route(ctx)
	if err != nil {
		return nil, err
	}
	st.v.Hold()
	return &driverpb.HoldResponse{}, nil
}

// SetVelocityTarget sets the body-frame velocity.
func (d *Daemon) SetVelocityTarget(
	ctx context.Context, req *driverpb.SetVelocityTargetRequest,
) (*driverpb.SetVelocityTargetResponse, error) {
	st, _, err := d.route(ctx)
	if err != nil {
		return nil, err
	}
	vel := req.GetVelocity()
	st.v.SetVelocity(float64(vel.GetXVel()), float64(vel.GetYVel()), float64(vel.GetZVel()), float64(vel.GetAngularVel()))
	return &driverpb.SetVelocityTargetResponse{}, nil
}

// SetGimbalAngleTarget moves the gimbal pitch. The mock only models offsets,
// so any angle mode applies the pitch as a change.
func (d *Daemon) SetGimbalAngleTarget(
	ctx context.Context, req *driverpb.SetGimbalAngleTargetRequest,
) (*driverpb.SetGimbalAngleTargetResponse, error) {
	st, _, err := d.route(ctx)
	if err != nil {
		return nil, err
	}
	st.v.SetGimbalPitch(float64(req.GetPose().GetPitch()))
	return &driverpb.SetGimbalAngleTargetResponse{}, nil
}

// missionFlow is the flow of a started mission: telemetry only, to a sink.
func missionFlow(vehicle string) []byte {
	ip, _ := fleet.SplitName(vehicle)
	sink := ip + ".mission_sink"
	b, _ := (&config.FlowConfig{
		Producers: []config.Producer{{Name: "telemetry"}},
		Engines:   []config.Engine{{Name: sink, Location: config.LocationLocal}},
		Edges:     []config.Edge{{From: "telemetry", To: sink, Timeout: "2s"}},
	}).ToBytes()
	return b
}

// StartMission replaces the flow with the mission one and starts the pattern.
func (d *Daemon) StartMission(
	ctx context.Context, req *missionpb.StartMissionRequest,
) (*missionpb.StartMissionResponse, error) {
	st, name, err := d.route(ctx)
	if err != nil {
		return nil, err
	}
	if err := d.setFlow(st, name, missionFlow(name)); err != nil {
		return nil, err
	}
	st.v.StartMission()
	return &missionpb.StartMissionResponse{}, nil
}

// StopMission stops the pattern, leaving the flow as it is.
func (d *Daemon) StopMission(
	ctx context.Context, req *missionpb.StopMissionRequest,
) (*missionpb.StopMissionResponse, error) {
	st, _, err := d.route(ctx)
	if err != nil {
		return nil, err
	}
	if !st.v.StopMission() {
		return nil, status.Error(codes.FailedPrecondition, "no mission loaded")
	}
	return &missionpb.StopMissionResponse{}, nil
}
