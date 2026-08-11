import time
import numpy as np
import os
import grpc
import cv2
import subprocess
import logging
from math import radians, degrees
from concurrent import futures
from xdg_base_dirs import xdg_runtime_dir
from pathlib import Path
# Simulator imports
from steeleagle_aviary.vehicle import Vehicle, DEFAULT_SPEED
# Panda3d imports
from panda3d.core import LVector3f, LPoint3f
# Utility imports
from steeleagle_aviary.util import calculate_bearing, rotate_body_to_neu, rotate_neu_to_body
from steeleagle_aviary.datatypes import GeodeticPoint, Mode, PoseMode
# Base import
from steeleagle_aviary.interfaces.base import Interface
# Protocol import
import steeleagle_protocol.v1.services.driver.control_pb2 as control_proto
from steeleagle_protocol.v1.services.driver.control_pb2_grpc import ControlServiceServicer, add_ControlServiceServicer_to_server
import steeleagle_protocol.v1.services.driver.stream_pb2 as stream_proto
from steeleagle_protocol.v1.services.driver.stream_pb2_grpc import StreamServiceServicer, add_StreamServiceServicer_to_server
import steeleagle_protocol.v1.services.driver.info_pb2 as info_proto
from steeleagle_protocol.v1.services.driver.info_pb2_grpc import InfoServiceServicer, add_InfoServiceServicer_to_server
import steeleagle_protocol.v1.messages.telemetry.telemetry_pb2 as telemetry_proto
import steeleagle_protocol.v1.common.common_pb2 as common_proto
from google.protobuf.timestamp_pb2 import Timestamp

"""SteelEagle Aviary interface."""

logger = logging.getLogger('Aviary/interfaces/steeleagle')

# SteelEagle directory
STEELEAGLE_DIR = 'steeleagle/plugins'
# Directory to place all vehicles
MAIN_DIR = 'aviary'
# Socket for hosting the server
SOCKET_ADDR = 'services.sock'
# Reported vehicle model, since this is a simulated vehicle
MODEL = 'aviary'

class SteelEagle(Interface):
    """Control interface wrapper."""
    def start(self):
        runtime_path = xdg_runtime_dir()
        if not runtime_path:
            raise ValueError('Runtime directory not set')
        path = Path(runtime_path) / STEELEAGLE_DIR / MAIN_DIR / self.vehicle.name
        os.makedirs(path, mode=0o777, exist_ok=True)
        path = path / SOCKET_ADDR
        logger.info(f'Listening on socket path {path}')
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
        add_ControlServiceServicer_to_server(Control(self.vehicle), self.server)
        add_StreamServiceServicer_to_server(Stream(self.vehicle), self.server)
        add_InfoServiceServicer_to_server(Info(), self.server)
        self.server.add_insecure_port(f'unix://{path}')
        self.server.start()
        logger.info(f'Server started!')

class Control(ControlServiceServicer):
    """gRPC control interface."""
    def __init__(self, vehicle: Vehicle):
        self.vehicle = vehicle

    def TakeOff(self, request, context):
        self.vehicle.set_position_target(self.vehicle.current_position() + LVector3f(0, 0, request.altitude))
        return control_proto.TakeOffResponse(
            expected_mode=telemetry_proto.Mode.MODE_LOITER,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_HOLDING,
        )

    def Land(self, request, context):
        current = self.vehicle.current_position()
        self.vehicle.set_position_target(LPoint3f(current.x, current.y, self.vehicle.sim_origin.z))
        return control_proto.LandResponse(
            expected_mode=telemetry_proto.Mode.MODE_LAND,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_STOPPED,
        )

    def Hold(self, request, context):
        self.vehicle.set_velocity_target(LVector3f(0, 0, 0))
        self.vehicle.set_pose_target(LVector3f(0, 0, 0), PoseMode.VELOCITY)
        return control_proto.HoldResponse(
            expected_mode=telemetry_proto.Mode.MODE_LOITER,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_HOLDING,
        )

    def Kill(self, request, context):
        # No-op in the simulator: there is no motor/crash physics to model, so
        # this just reports the expected mode/status without changing vehicle
        # state.
        return control_proto.KillResponse(
            expected_mode=telemetry_proto.Mode.MODE_EMERGENCY,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_STOPPED,
        )

    def ReturnToHome(self, request, context):
        geod_position = self.vehicle.current_geodetic_position()
        position = self.vehicle.current_position()
        target = self.vehicle.convert_to_geodetic(self.vehicle.sim_origin)
        bearing = calculate_bearing(
                geod_position.latitude,
                geod_position.longitude,
                target.latitude,
                target.longitude,
                )
        self.vehicle.set_pose_target(LVector3f(bearing, 0, 0), PoseMode.ANGLE)

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

    def SetRelativePositionTarget(self, request, context):
        theta = radians(self.vehicle.current_rotation().x)
        offset = LVector3f(request.position.x, request.position.y, request.position.z)
        pose = LPoint3f(request.position.angle, 0, 0)
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
        self.vehicle.set_pose_target(pose, mode=PoseMode.OFFSET)
        return control_proto.SetRelativePositionTargetResponse(
            setpoint=setpoint,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_HOLDING,
        )

    def SetGlobalPositionTarget(self, request, context):
        # If absolute altitude is specified, add the target altitude to the
        # current altitude
        if request.altitude_mode <= 1: # Relative mode
            altitude = request.position.altitude - self.vehicle.sim_origin.z
        else:
            altitude = self.vehicle.current_position().z

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
        # target; if it's START, face the explicitly provided heading instead
        if request.heading_mode <= 1:
            position = self.vehicle.current_geodetic_position()
            bearing = calculate_bearing(
                    position.latitude,
                    position.longitude,
                    request.position.latitude,
                    request.position.longitude
                    )
            self.vehicle.set_pose_target(LVector3f(bearing, 0, 0), PoseMode.ANGLE)
        else:
            self.vehicle.set_pose_target(LVector3f(request.position.heading, 0, 0), PoseMode.ANGLE)

        return control_proto.SetGlobalPositionTargetResponse(
            setpoint=request.position,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_HOLDING,
        )

    def SetVelocityTarget(self, request, context):
        if request.frame <= 1: # Body-aligned
            vector = LVector3f(request.velocity.x_vel, request.velocity.y_vel, request.velocity.z_vel)
        else: # NEU-aligned
            # sim space is ENU (x=east, y=north) but the proto's NEU frame is
            # (x=north, y=east), so the axes are swapped here.
            vector = LVector3f(request.velocity.y_vel, request.velocity.x_vel, request.velocity.z_vel)
        self.vehicle.set_velocity_target(vector, body_aligned=(request.frame <= 1))
        self.vehicle.set_pose_target(LVector3f(request.velocity.angular_vel, 0, 0), PoseMode.VELOCITY)
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

    def SetGimbalAngleTarget(self, request, context):
        # Frame of reference (body/NEU) doesn't change the simulated gimbal's
        # behavior, since there's no physical yaw axis to reference.
        if request.angle_mode == control_proto.AngleMode.ANGLE_MODE_OFFSET:
            pose_mode = PoseMode.OFFSET
        else:
            pose_mode = PoseMode.ANGLE
        self.vehicle.set_pose_target(LVector3f(
                request.pose.yaw,
                max(-90, min(90, request.pose.pitch)),
                request.pose.roll
                ), pose_mode
                )
        return control_proto.SetGimbalAngleTargetResponse(
            setpoint=common_proto.Pose(
                yaw=self.vehicle.pose_target.x,
                pitch=self.vehicle.pose_target.y,
                roll=self.vehicle.pose_target.z,
            ),
        )

    def SetGimbalVelocityTarget(self, request, context):
        self.vehicle.set_pose_target(LVector3f(
                request.pose_velocity.yaw_vel,
                request.pose_velocity.pitch_vel,
                request.pose_velocity.roll_vel
                ), PoseMode.VELOCITY
                )
        return control_proto.SetGimbalVelocityTargetResponse(setpoint=request.pose_velocity)

class Info(InfoServiceServicer):
    """gRPC info interface."""
    def GetVehicleInfo(self, request, context):
        return info_proto.GetVehicleInfoResponse(model=MODEL)

class Stream(StreamServiceServicer):
    """gRPC stream interface."""
    def __init__(self, vehicle: Vehicle):
        self.vehicle = vehicle

    def get_telemetry(self):
        # Get current position data
        rel_pos = self.vehicle.current_position()
        global_pos = self.vehicle.current_geodetic_position()
        vel_body = self.vehicle.get_velocity_body(1.0)
        angular_vel = self.vehicle.get_angular_velocity(1.0)
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
        velocity_body.angular_vel = angular_vel.x
        position_info = telemetry_proto.PositionInfo(
                home=home,
                global_position=global_position,
                relative_position=rel_position,
                velocity_body=velocity_body
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
        gimbal_velocity_neu.yaw_vel = angular_vel.x
        gimbal_velocity_neu.pitch_vel = angular_vel.y
        gimbal_velocity_neu.roll_vel = angular_vel.z
        gimbal_info = telemetry_proto.GimbalInfo(
                pose_body=gimbal_pose_body,
                pose_neu=gimbal_pose,
                angular_velocity_body=gimbal_velocity_body,
                angular_velocity_neu=gimbal_velocity_neu,
                )

        # No Alert Info needed, since there are no alerts to send
        return telemetry_proto.Telemetry(
                timestamp=Timestamp().GetCurrentTime(),
                battery_info=battery_info,
                gps_info=gps_info,
                position_info=position_info,
                gimbal_info=gimbal_info
                )

    #TODO: def GetVideoStreamURL(self, request, context):

    def StreamVideoFrames(self, request, context):
        sleep_time = 0.033 if not request.target_fps else (1.0 / request.target_fps)
        frame_id = 0
        while True:
            time.sleep(sleep_time)
            # Get frame data. Requested texture format is RGB, but some
            # pipes (p3tinydisplay's RAM readback in particular) return RGBA
            h_res = self.vehicle.camera.texture.get_x_size()
            v_res = self.vehicle.camera.texture.get_y_size()
            data = self.vehicle.camera.texture.getRamImage()
            arr = np.frombuffer(data, dtype=np.uint8)
            channels = arr.size // (v_res * h_res)
            arr = arr.reshape((v_res, h_res, channels))[:, :, :3]
            arr = np.flipud(arr)
            success, jpeg_data = cv2.imencode('.jpg', arr)
            if success:
                telemetry = self.get_telemetry()
                frame = telemetry_proto.EncodedFrame(
                        timestamp=Timestamp().GetCurrentTime(),
                        encoded_data=jpeg_data.tobytes(),
                        id=frame_id,
                        position_info=telemetry.position_info,
                        gimbal_info=telemetry.gimbal_info,
                        camera_id=0
                        )
                yield stream_proto.StreamVideoFramesResponse(
                    frame=frame
                )
                frame_id += 1

    def StreamTelemetry(self, request, context):
        sleep_time = 0.033 if not request.target_fps else (1.0 / request.target_fps)
        while True:
            time.sleep(sleep_time)
            yield stream_proto.StreamTelemetryResponse(telemetry=self.get_telemetry())
