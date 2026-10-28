import time
import threading
import numpy as np
import os
import grpc
import cv2
import logging
import uriel
from functools import wraps
from math import radians, degrees
from concurrent import futures
from pathlib import Path
# Simulator imports
from steeleagle_aviary.vehicle import Vehicle, DEFAULT_SPEED
# Panda3d imports
from panda3d.core import LVector3f, LPoint3f
# Utility imports
from steeleagle_aviary.util import calculate_bearing, rotate_body_to_neu, rotate_neu_to_body
from steeleagle_aviary.datatypes import GeodeticPoint, Mode, PoseMode
# Base import
from steeleagle_aviary.drivers.base import Driver
# Protocol import
import steeleagle_protocol.v1.services.driver.control_pb2 as control_proto
from steeleagle_protocol.v1.services.driver.control_pb2_grpc import ControlServiceServicer, add_ControlServiceServicer_to_server
import steeleagle_protocol.v1.services.driver.stream_pb2 as stream_proto
from steeleagle_protocol.v1.services.driver.stream_pb2_grpc import StreamServiceServicer, add_StreamServiceServicer_to_server
import steeleagle_protocol.v1.messages.telemetry.telemetry_pb2 as telemetry_proto
import steeleagle_protocol.v1.messages.frame.frame_pb2 as frame_proto
import steeleagle_protocol.v1.common.common_pb2 as common_proto
from google.protobuf.timestamp_pb2 import Timestamp
from google.protobuf.any_pb2 import Any

"""SteelEagle Aviary driver."""

logger = logging.getLogger('aviary/drivers/steeleagle')

# Name of the simulated camera, matching sim_vehicle.plugin.toml
CAMERA_NAME = 'rgb'
# Rates at which the camera and telemetry are published
CAMERA_FPS = 30
TELEMETRY_FPS = 30
# Fall speed of a killed vehicle, in m/s
KILL_FALL_SPEED = 10.0
# Uriel payload types of the published streams, as named in plugin configs
RAW_FRAME_TYPE = 'steeleagle_protocol/v1/messages/frame/RawFrame'
TELEMETRY_TYPE = 'steeleagle_protocol/v1/messages/telemetry/Telemetry'
# Room in an input for a RawFrame's fields besides its pixels
RAW_FRAME_HEADROOM = 16 << 10
# Room in an input for telemetry to grow, e.g. once a setpoint is set
TELEMETRY_HEADROOM = 4 << 10
# Frame dimensions (width, height) for each requested resolution; an
# unspecified resolution streams at the camera's native resolution
RESOLUTION_DIMENSIONS = {
    stream_proto.Resolution.RESOLUTION_360P: (640, 360),
    stream_proto.Resolution.RESOLUTION_480P: (854, 480),
    stream_proto.Resolution.RESOLUTION_720P: (1280, 720),
    stream_proto.Resolution.RESOLUTION_1080P: (1920, 1080),
    stream_proto.Resolution.RESOLUTION_4K: (3840, 2160),
}

def now() -> Timestamp:
    """Returns the current time as a timestamp."""
    # GetCurrentTime sets the timestamp in place and returns None
    ts = Timestamp()
    ts.GetCurrentTime()
    return ts

def setpoint(func):
    """Setpoint decorator.

    Automatically marks the setpoint for the decorated RPC method.
    """
    @wraps(func)
    def wrapper(self, request, context):
        resp = func(self, request, context)
        self.motion_info.mark_setpoint(resp.setpoint)
        return resp
    return wrapper

def nosetpoint(func):
    """No setpoint decorator.

    Clears the setpoint, if any is currently set.
    """
    @wraps(func)
    def wrapper(self, request, context):
        self.motion_info.mark_setpoint(None)
        return func(self, request, context)
    return wrapper

class SteelEagle(Driver):
    """Control interface wrapper."""
    def start(self):
        runtime_path = os.getenv('STEELEAGLE_SIMULATOR_DIR', None)
        qualifier = os.getenv('STEELEAGLE_LOCAL_QUALIFIER', '')
        if not runtime_path:
            raise ValueError('Runtime directory not set')
        # NOTE: the simulator doesn't callout so no client is created
        listen_sock = os.getenv('STEELEAGLE_SIMULATOR_LISTEN', None)
        if not listen_sock:
            raise ValueError('Listen socket not set')
        path = Path(runtime_path) / f'{qualifier}.{self.vehicle.name}'
        os.makedirs(path, mode=0o777, exist_ok=True)
        path = path / listen_sock
        # Remove a socket left over from a previous run, so that the daemon
        # doesn't connect to it before the server binds
        path.unlink(missing_ok=True)
        logger.info(f'Listening on socket path {path}')
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
        self.info = MotionInfo()
        add_ControlServiceServicer_to_server(Control(self.vehicle, self.info), self.server)
        add_StreamServiceServicer_to_server(Stream(self.vehicle, self.info), self.server)
        self.server.add_insecure_port(f'unix://{path}')
        self.server.start()
        logger.info(f'Server started!')

class MotionInfo:
    def __init__(self):
        self.setpoint = None
        self.mode = telemetry_proto.Mode.MODE_UNSPECIFIED

    def mark_setpoint(self, setpoint):
        self.setpoint = setpoint

    def set_flight_mode(self, mode: telemetry_proto.Mode):
        self.mode = mode

class Control(ControlServiceServicer):
    """gRPC control interface."""
    def __init__(self, vehicle: Vehicle, motion_info: MotionInfo):
        self.vehicle = vehicle
        self.motion_info = motion_info

    @nosetpoint
    def TakeOff(self, request, context):
        self.motion_info.set_flight_mode(telemetry_proto.Mode.MODE_TAKEOFF)
        self.vehicle.set_position_target(self.vehicle.current_position() + LVector3f(0, 0, request.altitude))
        return control_proto.TakeOffResponse(
            expected_mode=telemetry_proto.Mode.MODE_TAKEOFF,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_HOLDING,
        )

    @nosetpoint
    def Land(self, request, context):
        self.motion_info.set_flight_mode(telemetry_proto.Mode.MODE_LAND)
        current = self.vehicle.current_position()
        self.vehicle.set_position_target(LPoint3f(current.x, current.y, self.vehicle.sim_origin.z))
        return control_proto.LandResponse(
            expected_mode=telemetry_proto.Mode.MODE_LAND,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_STOPPED,
        )

    @nosetpoint
    def Hold(self, request, context):
        self.motion_info.set_flight_mode(telemetry_proto.Mode.MODE_LOITER)
        self.vehicle.set_velocity_target(LVector3f(0, 0, 0))
        self.vehicle.set_heading_target(0, PoseMode.VELOCITY)
        self.vehicle.set_gimbal_target(0, 0, PoseMode.VELOCITY)
        return control_proto.HoldResponse(
            expected_mode=telemetry_proto.Mode.MODE_LOITER,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_HOLDING,
        )

    @nosetpoint
    def Kill(self, request, context):
        self.motion_info.set_flight_mode(telemetry_proto.Mode.MODE_EMERGENCY)
        # Cutting the motors drops the vehicle straight down, and the gimbal
        # stops wherever it is
        current = self.vehicle.current_position()
        self.vehicle.set_position_target(LPoint3f(current.x, current.y, self.vehicle.sim_origin.z), speed=KILL_FALL_SPEED)
        self.vehicle.set_heading_target(0, PoseMode.VELOCITY)
        self.vehicle.set_gimbal_target(0, 0, PoseMode.VELOCITY)
        return control_proto.KillResponse(
            expected_mode=telemetry_proto.Mode.MODE_EMERGENCY,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_STOPPED,
        )

    @nosetpoint
    def ReturnToHome(self, request, context):
        self.motion_info.set_flight_mode(telemetry_proto.Mode.MODE_RETURN_TO_HOME)
        geod_position = self.vehicle.current_geodetic_position()
        position = self.vehicle.current_position()
        target = self.vehicle.convert_to_geodetic(self.vehicle.sim_origin)
        bearing = calculate_bearing(
                geod_position.latitude,
                geod_position.longitude,
                target.latitude,
                target.longitude,
                )
        self.vehicle.set_heading_target(bearing, PoseMode.ANGLE)

        altitude = request.min_return_altitude + self.vehicle.sim_origin.z
        if request.min_return_altitude < position.z:
            altitude = position.z

        final = self.vehicle.sim_origin.z
        if request.end_behavior <= 1:
            final = self.vehicle.sim_origin.z + request.final_altitude
            expected_mode = telemetry_proto.Mode.MODE_LOITER
            expected_status = telemetry_proto.MotionStatus.MOTION_STATUS_HOLDING
        else:
            expected_mode = telemetry_proto.Mode.MODE_LAND
            expected_status = telemetry_proto.MotionStatus.MOTION_STATUS_STOPPED

        waypoints = [
            LPoint3f(position.x, position.y, altitude),
            LPoint3f(self.vehicle.sim_origin.x, self.vehicle.sim_origin.y, altitude),
            LPoint3f(self.vehicle.sim_origin.x, self.vehicle.sim_origin.y, final),
        ]

        self.vehicle.set_waypoint_target(waypoints)
        return control_proto.ReturnToHomeResponse(
            expected_mode=expected_mode,
            expected_status=expected_status,
        )

    @setpoint
    def SetRelativePositionTarget(self, request, context):
        self.motion_info.set_flight_mode(telemetry_proto.Mode.MODE_GUIDED)
        theta = radians(self.vehicle.current_rotation().x)
        offset = LVector3f(request.position.x, request.position.y, request.position.z)
        if request.frame <= 1: # Body-aligned
            self.vehicle.set_relative_position_target(offset, body_aligned=True, speed=request.speed or DEFAULT_SPEED)
            north, east = rotate_body_to_neu(request.position.x, request.position.y, theta)
            setpoint = common_proto.RelativePosition(
                x=north,
                y=east,
                z=request.position.z,
                angle=degrees(theta) + request.position.angle,
            )
        else: # NEU-aligned
            # sim space is ENU (x=east, y=north) but the proto's NEU frame is
            # (x=north, y=east), so the axes are swapped here.
            neu_offset = LVector3f(request.position.y, request.position.x, request.position.z)
            self.vehicle.set_relative_position_target(neu_offset, body_aligned=False, speed=request.speed or DEFAULT_SPEED)
            setpoint = request.position
        self.vehicle.set_heading_target(request.position.angle, PoseMode.OFFSET)
        return control_proto.SetRelativePositionTargetResponse(
            setpoint=setpoint,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_HOLDING,
        )

    @setpoint
    def SetGlobalPositionTarget(self, request, context):
        self.motion_info.set_flight_mode(telemetry_proto.Mode.MODE_GUIDED)
        # convert_to_sim/convert_to_geodetic treat sim Z and geodetic
        # altitude as the same value, so a relative (above-takeoff) request
        # needs sim_origin.z added to become absolute, while an absolute
        # request is already in the right frame.
        if request.altitude_mode <= 1: # Relative mode
            altitude = request.position.altitude + self.vehicle.sim_origin.z
        else: # Absolute mode
            altitude = request.position.altitude

        self.vehicle.set_position_target(self.vehicle.convert_to_sim(
                GeodeticPoint(
                    request.position.latitude,
                    request.position.longitude,
                    altitude
                    )
                ),
                speed=request.speed or DEFAULT_SPEED,
                )

        # If heading mode is TO_TARGET, set the pose to look at the position
        # target; if it's START, face the explicitly provided heading instead.
        if request.heading_mode <= 1:
            position = self.vehicle.current_geodetic_position()
            heading = calculate_bearing(
                    position.latitude,
                    position.longitude,
                    request.position.latitude,
                    request.position.longitude
                    )
        else:
            heading = request.position.heading
        self.vehicle.set_heading_target(heading, PoseMode.ANGLE)

        setpoint = common_proto.GlobalPosition(
            latitude=request.position.latitude,
            longitude=request.position.longitude,
            altitude=altitude,
            heading=heading,
        )
        return control_proto.SetGlobalPositionTargetResponse(
            setpoint=setpoint,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_HOLDING,
        )

    @setpoint
    def SetVelocityTarget(self, request, context):
        self.motion_info.set_flight_mode(telemetry_proto.Mode.MODE_GUIDED)
        if request.frame <= 1: # Body-aligned
            vector = LVector3f(request.velocity.x_vel, request.velocity.y_vel, request.velocity.z_vel)
        else: # NEU-aligned
            # sim space is ENU (x=east, y=north) but the proto's NEU frame is
            # (x=north, y=east), so the axes are swapped here.
            vector = LVector3f(request.velocity.y_vel, request.velocity.x_vel, request.velocity.z_vel)
        self.vehicle.set_velocity_target(vector, body_aligned=(request.frame <= 1))
        self.vehicle.set_heading_target(request.velocity.angular_vel, PoseMode.VELOCITY)
        if request.frame <= 1: # Body-aligned
            theta = radians(self.vehicle.current_rotation().x)
            north_vel, east_vel = rotate_body_to_neu(request.velocity.x_vel, request.velocity.y_vel, theta)
            setpoint = common_proto.Velocity(
                x_vel=north_vel,
                y_vel=east_vel,
                z_vel=request.velocity.z_vel,
                angular_vel=request.velocity.angular_vel,
            )
        else: # NEU-aligned
            setpoint = request.velocity
        return control_proto.SetVelocityTargetResponse(
            setpoint=setpoint,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_IN_TRANSIT,
        )

    @setpoint
    def SetGimbalAngleTarget(self, request, context):
        if request.angle_mode == control_proto.AngleMode.ANGLE_MODE_OFFSET:
            pose_mode = PoseMode.OFFSET
        else:
            pose_mode = PoseMode.ANGLE
        self.vehicle.set_gimbal_target(
                request.pose.pitch,
                request.pose.roll,
                pose_mode
                )
        # Clamp after any offset is applied, so the gimbal can't pass straight
        # down or up
        self.vehicle.gimbal_target.y = max(-90, min(90, self.vehicle.gimbal_target.y))
        setpoint = common_proto.Pose(
            pitch=self.vehicle.gimbal_target.y,
            roll=self.vehicle.gimbal_target.z,
        )
        # The simulated gimbal has no yaw axis of its own, since the camera is
        # the vehicle, so yaw relative to the body is always zero and a body
        # frame yaw is ignored; a NEU yaw is a heading, so turns the vehicle
        if request.frame == control_proto.ReferenceFrame.REFERENCE_FRAME_NEU:
            self.vehicle.set_heading_target(request.pose.yaw, pose_mode)
            setpoint.yaw = self.vehicle.heading_setpoint()
        return control_proto.SetGimbalAngleTargetResponse(setpoint=setpoint)

    @setpoint
    def SetGimbalVelocityTarget(self, request, context):
        # Gimbal velocities are relative to the body, and the simulated gimbal
        # has no yaw axis of its own, so yaw is ignored rather than turning
        # the vehicle
        self.vehicle.set_gimbal_target(
                request.pose_velocity.pitch_vel,
                request.pose_velocity.roll_vel,
                PoseMode.VELOCITY
                )
        return control_proto.SetGimbalVelocityTargetResponse(
            setpoint=common_proto.PoseVelocity(
                pitch_vel=request.pose_velocity.pitch_vel,
                roll_vel=request.pose_velocity.roll_vel,
            ),
        )

class Publisher:
    """Publishes on a Uriel producer from its own thread.

    Calls `produce` at `fps` and publishes the bytes it returns, if any, under
    the producer `uri` with the given payload type. The producer is created
    lazily, sized to the first input, and recreated if inputs grow, since a
    Uriel producer must be created, used and closed on one thread.
    """
    def __init__(self, uri: str, payload_type: str, fps: float, produce, headroom: int = 0):
        self.uri = uri
        self.payload_type = payload_type
        self.period = 1.0 / fps
        self.produce = produce
        self.headroom = headroom
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, name=f'publisher [{uri}]', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        self.thread.join()

    def run(self):
        producer = None
        max_input_size = 0
        next_time = time.monotonic()
        try:
            while not self.stopped.is_set():
                try:
                    data = self.produce()
                    if data is not None:
                        if len(data) > max_input_size:
                            if producer is not None:
                                producer.close()
                            max_input_size = len(data) + self.headroom
                            producer = uriel.Producer(
                                    self.uri,
                                    input_type=self.payload_type,
                                    max_input_size=max_input_size,
                                    )
                            logger.info(f'Publishing {self.payload_type} on {self.uri}, up to {max_input_size} bytes each')
                        producer.send(data)
                except uriel.AgainError:
                    pass # No free input buffer; newer inputs follow
                except Exception as e:
                    logger.warning(f'Failed to publish on {self.uri}, reason: {e}')
                next_time += self.period
                sleep_time = next_time - time.monotonic()
                if sleep_time > 0:
                    self.stopped.wait(sleep_time)
                else: # Overran the period
                    next_time = time.monotonic()
        finally:
            if producer is not None:
                producer.close()

class Stream(StreamServiceServicer):
    """gRPC stream interface."""
    def __init__(self, vehicle: Vehicle, motion_info: MotionInfo):
        self.vehicle = vehicle
        self.motion_info = motion_info
        # Streams replace those started before them, under this lock
        self.lock = threading.Lock()
        self.camera_publisher = None
        self.telemetry_publisher = None
        self.frame_id = 0

    def get_telemetry(self):
        # Get current position data
        rel_pos = self.vehicle.current_position()
        global_pos = self.vehicle.current_geodetic_position()
        # The velocity is read once, since reading it can advance waypoints
        vel = self.vehicle.get_velocity(1.0)
        vel_body = self.vehicle.to_body(vel)
        angular_vel = self.vehicle.get_angular_velocity(1.0)
        # Simulator angles turn counter-clockwise but headings clockwise, so
        # the yaw rate is negated to match the heading
        yaw_vel = -angular_vel.x
        pose = self.vehicle.current_pose()
        pose_body = self.vehicle.current_pose_body()

        # Build telemetry object
        # Battery Info
        battery_info = telemetry_proto.BatteryInfo(percentage=100)

        # GPS Info
        gps_info = telemetry_proto.GpsInfo(satellites=15)

        # Position Info
        # Home
        home = common_proto.GlobalPosition()
        home.latitude = self.vehicle.origin.latitude
        home.longitude = self.vehicle.origin.longitude
        home.altitude = self.vehicle.origin.altitude
        # Global position
        global_position = common_proto.GlobalPosition()
        global_position.latitude = global_pos.latitude
        global_position.longitude = global_pos.longitude
        global_position.altitude = global_pos.altitude
        global_position.heading = pose.x
        # Relative position
        rel_position = common_proto.RelativePosition()
        rel_position.x = rel_pos.x
        rel_position.y = rel_pos.y
        rel_position.z = rel_pos.z - self.vehicle.sim_origin.z
        # Body velocity
        velocity_body = common_proto.Velocity()
        velocity_body.x_vel = vel_body.x
        velocity_body.y_vel = vel_body.y
        velocity_body.z_vel = vel_body.z
        velocity_body.angular_vel = yaw_vel
        # NEU velocity; sim space is ENU (x=east, y=north)
        velocity_neu = common_proto.Velocity()
        velocity_neu.x_vel = vel.y
        velocity_neu.y_vel = vel.x
        velocity_neu.z_vel = vel.z
        velocity_neu.angular_vel = yaw_vel
        setpoint = Any()
        if self.motion_info.setpoint:
            setpoint.Pack(self.motion_info.setpoint)
        position_info = telemetry_proto.PositionInfo(
                home=home,
                global_position=global_position,
                relative_position=rel_position,
                velocity_body=velocity_body,
                velocity_neu=velocity_neu,
                setpoint=setpoint,
                )

        # Gimbal Info
        gimbal_pose_body = common_proto.Pose()
        gimbal_pose_body.yaw = pose_body.x
        gimbal_pose_body.pitch = pose_body.y
        gimbal_pose_body.roll = pose_body.z
        gimbal_pose = common_proto.Pose()
        gimbal_pose.yaw = pose.x
        gimbal_pose.pitch = pose.y
        gimbal_pose.roll = pose.z
        gimbal_velocity_body = common_proto.PoseVelocity()
        gimbal_velocity_body.yaw_vel = 0.0
        gimbal_velocity_body.pitch_vel = angular_vel.y
        gimbal_velocity_body.roll_vel = angular_vel.z
        gimbal_velocity_neu = common_proto.PoseVelocity()
        gimbal_velocity_neu.yaw_vel = yaw_vel
        gimbal_velocity_neu.pitch_vel = angular_vel.y
        gimbal_velocity_neu.roll_vel = angular_vel.z
        gimbal_setpoint = Any()
        if self.motion_info.setpoint:
            gimbal_setpoint.Pack(self.motion_info.setpoint)
        gimbal_info = telemetry_proto.GimbalInfo(
                pose_body=gimbal_pose_body,
                pose_neu=gimbal_pose,
                angular_velocity_body=gimbal_velocity_body,
                angular_velocity_neu=gimbal_velocity_neu,
                gimbal_setpoint=gimbal_setpoint,
                )

        motion_status = telemetry_proto.MotionStatus.MOTION_STATUS_STOPPED
        if vel_body.length() > 0:
            motion_status = telemetry_proto.MotionStatus.MOTION_STATUS_IN_TRANSIT
        elif rel_position.z > 0:
            motion_status = telemetry_proto.MotionStatus.MOTION_STATUS_HOLDING

        mode = self.motion_info.mode

        # No Alert Info needed, since there are no alerts to send
        return telemetry_proto.Telemetry(
                timestamp=now(),
                battery_info=battery_info,
                gps_info=gps_info,
                position_info=position_info,
                gimbal_info=gimbal_info,
                motion_status=motion_status,
                mode=mode,
                )

    def get_frame(self, dimensions) -> bytes | None:
        """Reads the camera texture as a serialized RawFrame, or None if the
        camera hasn't rendered yet."""
        # Requested texture format is RGB, but some pipes (p3tinydisplay's
        # RAM readback in particular) return RGBA. Panda3D stores RAM images
        # as BGR(A), bottom row first.
        texture = self.vehicle.camera.texture
        h_res = texture.get_x_size()
        v_res = texture.get_y_size()
        data = texture.getRamImage()
        if not h_res or not v_res or not data:
            return None
        arr = np.frombuffer(data, dtype=np.uint8)
        channels = arr.size // (v_res * h_res)
        arr = arr.reshape((v_res, h_res, channels))[:, :, :3]
        arr = cv2.cvtColor(np.flipud(arr), cv2.COLOR_BGR2RGB)
        if dimensions is not None and (arr.shape[1], arr.shape[0]) != dimensions:
            arr = cv2.resize(arr, dimensions, interpolation=cv2.INTER_AREA)
        telemetry = self.get_telemetry()
        frame = frame_proto.RawFrame(
                timestamp=now(),
                data=arr.tobytes(),
                h_res=arr.shape[1],
                v_res=arr.shape[0],
                channels=3,
                id=self.frame_id,
                position_info=telemetry.position_info,
                gimbal_info=telemetry.gimbal_info,
                camera_id=0,
                )
        self.frame_id += 1
        return frame.SerializeToString()

    def StreamCameras(self, request, context):
        # This plugin only has one camera, so it is streamed if it is
        # requested, and stopped otherwise. Any other camera is an error.
        requests = list(request.secondaries)
        if request.HasField('primary'):
            requests.append(request.primary)
        unknown = [r.id for r in requests if r.id != CAMERA_NAME]
        if unknown:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, f'unknown cameras {unknown}, only {CAMERA_NAME!r} exists')
        camera = requests[0] if requests else None
        with self.lock:
            if self.camera_publisher is not None:
                self.camera_publisher.stop()
                self.camera_publisher = None
            if camera is not None:
                dimensions = None
                if camera.HasField('target_resolution'):
                    dimensions = RESOLUTION_DIMENSIONS.get(camera.target_resolution)
                self.camera_publisher = Publisher(
                        camera.uri,
                        RAW_FRAME_TYPE,
                        CAMERA_FPS,
                        lambda: self.get_frame(dimensions),
                        headroom=RAW_FRAME_HEADROOM,
                        )
        return stream_proto.StreamCamerasResponse()

    def StreamTelemetry(self, request, context):
        if not request.uri:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, 'no telemetry uri given')
        with self.lock:
            if self.telemetry_publisher is not None:
                self.telemetry_publisher.stop()
            self.telemetry_publisher = Publisher(
                    request.uri,
                    TELEMETRY_TYPE,
                    TELEMETRY_FPS,
                    lambda: self.get_telemetry().SerializeToString(),
                    headroom=TELEMETRY_HEADROOM,
                    )
        return stream_proto.StreamTelemetryResponse()
