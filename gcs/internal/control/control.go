// Package control serves the web UI's vehicle control endpoints. Each
// command is sent to every vehicle it names at once, each through the daemon,
// which routes a call to a vehicle by the vehicle's name in its x-route-key
// metadata.
package control

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sync"
	"time"

	commonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/common"
	driverpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/driver"
	missionpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/mission"
	"github.com/rs/zerolog"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const (
	// routeHeader is the metadata key the daemon routes calls to vehicles
	// by.
	routeHeader = "x-route-key"
	// maxRequestSize is the largest request body accepted.
	maxRequestSize = 64 << 10
	// DefaultTimeout is how long a call to a vehicle may take.
	DefaultTimeout = 10 * time.Second
	// HoldTimeout is how long each call of a hold may take: a vehicle that
	// can't be reached shouldn't keep the operator waiting.
	HoldTimeout = 3 * time.Second
	// JoystickTimeout is how long each velocity or gimbal call may take: the
	// browser sends the next one only once every vehicle has answered, so a
	// vehicle that can't be reached mustn't stall manual flight of the
	// others.
	JoystickTimeout = 750 * time.Millisecond
)

// Result is the outcome of a call to a vehicle.
type Result struct {
	Vehicle string `json:"vehicle"`
	// Command is the call, e.g. "TakeOff".
	Command string `json:"command"`
	Success bool   `json:"success"`
	// Details says why the call failed, if it did.
	Details string `json:"details"`
	// code is the gRPC status code of the failure, or OK.
	code codes.Code
}

// Response is the response to every control request: the result of each
// call, in the order of the request's vehicles.
type Response struct {
	Results []Result `json:"results"`
}

// call is one call to a vehicle, made with a context routed to it.
type call struct {
	name string
	do   func(ctx context.Context) error
	// timeout is how long the call may take; zero means the controller's.
	timeout time.Duration
}

// Controller sends commands to vehicles through the daemon.
type Controller struct {
	control driverpb.ControlServiceClient
	mission missionpb.MissionServiceClient
	timeout time.Duration
	log     zerolog.Logger
	onHold  func(vehicles []string)
}

// New returns a controller that calls the daemon on conn, each call taking at
// most timeout. A nil conn makes every request fail, for a GCS the daemon
// gave no socket.
func New(conn grpc.ClientConnInterface, timeout time.Duration, log zerolog.Logger) *Controller {
	c := &Controller{timeout: timeout, log: log}
	if conn != nil {
		c.control = driverpb.NewControlServiceClient(conn)
		c.mission = missionpb.NewMissionServiceClient(conn)
	}
	return c
}

// OnHold sets a hook called after each hold with the vehicles whose Hold
// didn't fail for lack of an answer (unavailable, timed out or canceled), in
// the order of the request. It isn't called when there are none. The hook is
// called synchronously once the results are computed, even if the browser went
// away; it must return quickly, as the response isn't delivered until it
// returns. Set it before serving.
func (c *Controller) OnHold(hook func(vehicles []string)) { c.onHold = hook }

// run makes calls on each of vehicles, at once for every vehicle and in order
// for each one, whether or not the ones before succeed.
func (c *Controller) run(ctx context.Context, vehicles []string, calls ...call) []Result {
	results := make([][]Result, len(vehicles))
	var wg sync.WaitGroup
	for i, vehicle := range vehicles {
		wg.Go(func() {
			routed := metadata.AppendToOutgoingContext(ctx, routeHeader, vehicle)
			for _, call := range calls {
				results[i] = append(results[i], c.do(routed, vehicle, call))
			}
		})
	}
	wg.Wait()
	var all []Result
	for _, r := range results {
		all = append(all, r...)
	}
	return all
}

// do makes call on vehicle.
func (c *Controller) do(ctx context.Context, vehicle string, call call) Result {
	timeout := c.timeout
	if call.timeout != 0 {
		timeout = call.timeout
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	result := Result{Vehicle: vehicle, Command: call.name, Success: true}
	if err := call.do(ctx); err != nil {
		result.Success, result.Details, result.code = false, status.Convert(err).Message(), status.Code(err)
		if call.name == "StopMission" && result.code == codes.FailedPrecondition {
			// There was no mission to stop, which is what was asked for
			result.Success, result.Details, result.code = true, "", codes.OK
		}
		if !result.Success {
			c.log.Warn().Err(err).Str("vehicle", vehicle).Str("command", call.name).Msg("command failed")
		} else {
			c.log.Info().Str("vehicle", vehicle).Str("command", call.name).Msg("no mission to stop")
		}
	} else {
		c.log.Info().Str("vehicle", vehicle).Str("command", call.name).Msg("command sent")
	}
	return result
}

// StartRequest starts the vehicles' missions.
type StartRequest struct {
	Vehicles []string `json:"vehicles"`
}

// CommandRequest sends the vehicles one command: the one field it sets
// besides Vehicles.
type CommandRequest struct {
	// TakeOff takes off to an altitude [meters].
	TakeOff *float64 `json:"takeoff"`
	Land    *bool    `json:"land"`
	// Rth returns to home.
	Rth *bool `json:"rth"`
	// Hold holds position, and stops the mission: the operator takes over.
	Hold        *bool `json:"hold"`
	StopMission *bool `json:"stop_mission"`
	// Arm isn't supported by vehicles.
	Arm      *bool    `json:"arm"`
	Vehicles []string `json:"vehicles"`
}

// JoystickRequest sets the vehicles' velocity targets [meters/s, and
// degrees/s for AngularVel].
type JoystickRequest struct {
	XVel       float32 `json:"xvel"`
	YVel       float32 `json:"yvel"`
	ZVel       float32 `json:"zvel"`
	AngularVel float32 `json:"angularvel"`
	// Duration is unused: a velocity target lasts until the next one.
	Duration int      `json:"duration"`
	Vehicles []string `json:"vehicles"`
}

// GimbalRequest moves the vehicles' gimbals by offsets [degrees].
type GimbalRequest struct {
	Pitch    float32  `json:"pitch"`
	Yaw      float32  `json:"yaw"`
	Roll     float32  `json:"roll"`
	Vehicles []string `json:"vehicles"`
}

// ServeStart starts missions.
func (c *Controller) ServeStart(w http.ResponseWriter, r *http.Request) {
	var req StartRequest
	if !c.decode(w, r, &req, &req.Vehicles) {
		return
	}
	c.respond(w, r, req.Vehicles, call{name: "StartMission", do: func(ctx context.Context) error {
		_, err := c.mission.StartMission(ctx, &missionpb.StartMissionRequest{})
		return err
	}})
}

// ServeCommand sends a command.
func (c *Controller) ServeCommand(w http.ResponseWriter, r *http.Request) {
	var req CommandRequest
	if !c.decode(w, r, &req, &req.Vehicles) {
		return
	}
	set := 0
	for _, field := range []bool{req.TakeOff != nil, req.Land != nil, req.Rth != nil,
		req.Hold != nil, req.StopMission != nil, req.Arm != nil} {
		if field {
			set++
		}
	}
	if set != 1 {
		writeError(w, http.StatusBadRequest, "set exactly one of takeoff, land, rth, hold, stop_mission and arm")
		return
	}

	takeOff := call{name: "TakeOff", do: func(ctx context.Context) error {
		altitude := float32(*req.TakeOff)
		_, err := c.control.TakeOff(ctx, driverpb.TakeOffRequest_builder{Altitude: &altitude}.Build())
		return err
	}}
	land := call{name: "Land", do: func(ctx context.Context) error {
		_, err := c.control.Land(ctx, &driverpb.LandRequest{})
		return err
	}}
	rth := call{name: "ReturnToHome", do: func(ctx context.Context) error {
		_, err := c.control.ReturnToHome(ctx, &driverpb.ReturnToHomeRequest{})
		return err
	}}
	hold := call{name: "Hold", timeout: HoldTimeout, do: func(ctx context.Context) error {
		_, err := c.control.Hold(ctx, &driverpb.HoldRequest{})
		return err
	}}
	stop := call{name: "StopMission", do: func(ctx context.Context) error {
		_, err := c.mission.StopMission(ctx, &missionpb.StopMissionRequest{})
		return err
	}}

	switch {
	case req.TakeOff != nil:
		if *req.TakeOff < 0 {
			writeError(w, http.StatusBadRequest, fmt.Sprintf("takeoff altitude %v is negative", *req.TakeOff))
			return
		}
		c.respond(w, r, req.Vehicles, takeOff)
	case req.Arm != nil:
		writeError(w, http.StatusBadRequest, "vehicles can't be armed or disarmed; they arm themselves on take off")
	case isTrue(req.Land):
		c.respond(w, r, req.Vehicles, land)
	case isTrue(req.Rth):
		c.respond(w, r, req.Vehicles, rth)
	case isTrue(req.Hold):
		// Holding first takes manual control; the mission is stopped even if
		// the vehicle can't hold, so it can't fly it any further
		stopFast := stop
		stopFast.timeout = HoldTimeout
		// A browser that goes away mustn't cancel the hold or skip stopping
		// the mission; each call is bounded by HoldTimeout
		results := c.run(context.WithoutCancel(r.Context()), req.Vehicles, hold, stopFast)
		c.afterHold(results)
		if r.Context().Err() == nil {
			writeJSON(w, http.StatusOK, Response{Results: results})
		}
	case isTrue(req.StopMission):
		c.respond(w, r, req.Vehicles, stop)
	default:
		// A command set to false sends nothing
		writeJSON(w, http.StatusOK, Response{Results: []Result{}})
	}
}

func isTrue(b *bool) bool { return b != nil && *b }

// ServeJoystick sets velocity targets.
func (c *Controller) ServeJoystick(w http.ResponseWriter, r *http.Request) {
	var req JoystickRequest
	if !c.decode(w, r, &req, &req.Vehicles) {
		return
	}
	velocity := commonpb.Velocity_builder{
		XVel: &req.XVel, YVel: &req.YVel, ZVel: &req.ZVel, AngularVel: &req.AngularVel,
	}.Build()
	c.respond(w, r, req.Vehicles, call{name: "SetVelocityTarget", timeout: JoystickTimeout, do: func(ctx context.Context) error {
		_, err := c.control.SetVelocityTarget(ctx, driverpb.SetVelocityTargetRequest_builder{Velocity: velocity}.Build())
		return err
	}})
}

// ServeGimbal moves gimbals.
func (c *Controller) ServeGimbal(w http.ResponseWriter, r *http.Request) {
	var req GimbalRequest
	if !c.decode(w, r, &req, &req.Vehicles) {
		return
	}
	pose := commonpb.Pose_builder{Pitch: &req.Pitch, Yaw: &req.Yaw, Roll: &req.Roll}.Build()
	mode := driverpb.AngleMode_ANGLE_MODE_OFFSET
	c.respond(w, r, req.Vehicles, call{name: "SetGimbalAngleTarget", timeout: JoystickTimeout, do: func(ctx context.Context) error {
		_, err := c.control.SetGimbalAngleTarget(ctx,
			driverpb.SetGimbalAngleTargetRequest_builder{Pose: pose, AngleMode: &mode}.Build())
		return err
	}})
}

// decode reads a JSON request into req, whose vehicles it checks and
// deduplicates, or writes why it can't.
func (c *Controller) decode(w http.ResponseWriter, r *http.Request, req any, vehicles *[]string) bool {
	if c.control == nil {
		writeError(w, http.StatusServiceUnavailable, "the GCS has no connection to the daemon")
		return false
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxRequestSize)).Decode(req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request: "+err.Error())
		return false
	}
	seen := map[string]bool{}
	unique := (*vehicles)[:0]
	for _, v := range *vehicles {
		if v == "" {
			writeError(w, http.StatusBadRequest, "a vehicle has no name")
			return false
		}
		if !seen[v] {
			seen[v] = true
			unique = append(unique, v)
		}
	}
	if len(unique) == 0 {
		writeError(w, http.StatusBadRequest, "no vehicles")
		return false
	}
	*vehicles = unique
	return true
}

// respond makes calls on vehicles, and writes their results.
func (c *Controller) respond(w http.ResponseWriter, r *http.Request, vehicles []string, calls ...call) {
	results := c.run(r.Context(), vehicles, calls...)
	if err := r.Context().Err(); errors.Is(err, context.Canceled) {
		return // the browser went away
	}
	writeJSON(w, http.StatusOK, Response{Results: results})
}

// afterHold calls the hold hook with the vehicles whose Hold got an answer.
func (c *Controller) afterHold(results []Result) {
	if c.onHold == nil {
		return
	}
	var held []string
	for _, r := range results {
		if r.Command == "Hold" && r.code != codes.Unavailable && r.code != codes.DeadlineExceeded && r.code != codes.Canceled {
			held = append(held, r.Vehicle)
		}
	}
	if len(held) > 0 {
		c.onHold(held)
	}
}

// writeError writes an error the way the web UI reads it.
func writeError(w http.ResponseWriter, code int, detail string) {
	writeJSON(w, code, map[string]string{"detail": detail})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
