import time
import numpy as np
import os
import grpc
import cv2
import subprocess
import logging
from concurrent import futures
from xdg_base_dirs import xdg_runtime_dir
from pathlib import Path
# Simulator imports
from steeleagle_aviary.vehicle import Vehicle
# Panda3d imports
from panda3d.core import LVector3f, LPoint3f
# Utility imports
from steeleagle_aviary.util import calculate_bearing
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
        return control_proto.TakeOffResponse()

    def Land(self, request, context):
        current = self.vehicle.current_position()
        self.vehicle.set_position_target(LPoint3f(current.x, current.y, self.vehicle.sim_origin.z))
        return control_proto.LandResponse()

    def Hold(self, request, context):
        self.vehicle.set_velocity_target(LVector3f(0, 0, 0))
        self.vehicle.set_pose_target(LVector3f(0, 0, 0), PoseMode.VELOCITY)
        return control_proto.HoldResponse()

    def SetHome(self, request, context):
        self.vehicle.origin.latitude = request.new_home.latitude
        self.vehicle.origin.longitude = request.new_home.longitude
        return control_proto.SetHomeResponse()

    def ReturnToHome(self, request, context):
        geod_position = self.vehicle.current_geodetic_position()
        position = self.vehicle.current_position()
        target = self.vehicle.convert_to_geodetic(
                self.vehicle.get_sim_origin(),
                )
        bearing = calculate_bearing(
                geod_position.latitude,
                geod_position.longitude,
                target.latitude,
                target.longitude,
                )
        self.vehicle.set_pose_target(LVector3f(bearing, 0, 0), PoseMode.ANGLE)

        altitude = request.min_return_altitude + self.vehicle.sim_origin.z
        if request.min_return_altitude < self.vehicle.position.z:
            altitude = self.vehicle.position.z

        final = self.sim_origin.z
        if request.end_behavior <= 1:
            final = self.sim_origin.z + request.final_altitude

        waypoints = [
            LPoint3f(position.x, position.y, altitude),
            LPoint3f(self.vehicle.sim_origin.x, self.vehicle.sim_origin.y, altitude),
            LPoint3f(self.vehicle.sim_origin.x, self.vehicle.sim_origin.y, final),
        ]
        
        self.vehicle.set_waypoint_target(waypoints)
        return control_proto.ReturnToHomeResponse()

    def GoToRelativePosition(self, request, context):
        offset = LVector3f(request.position.x, request.position.y, request.position.z)
        pose = LPoint3f(request.position.angle, 0, 0)
        body_aligned = request.frame <= 1
        self.vehicle.set_relative_position_target(new_position, body_aligned=body_aligned, speed=request.speed)
        self.vehicle.set_pose_target(pose, mode=PoseMode.OFFSET, speed=request.angular_speed)
        return control_proto.GoToRelativePositionResponse()

    def GoToGlobalPosition(self, request, context):
        # If absolute altitude is specified, add the target altitude
        # to the current altitude
        if request.altitude_mode <= 1: # Relative mode
            altitude = request.position.altitude - self.vehicle.sim_origin.z
        else:
            altitude = position.z

        self.vehicle.set_position_target(self.vehicle.convert_to_sim(
                GeodeticPoint(
                    request.position.latitude,
                    request.position.longitude,
                    altitude
                    ),
                speed=request.speed,
                ))

        # If heading mode is TO_TARGET, set the pose to look at the
        # position target
        if request.heading_mode <= 1:
            position = self.vehicle.current_geodetic_position()
            bearing = calculate_bearing(
                    position.latitude,
                    position.longitude,
                    request.position.latitude,
                    request.position.longitude
                    )
            self.vehicle.set_pose_target(LVector3f(bearing, 0, 0), PoseMode.ANGLE, speed=request.angular_speed)

        return control_proto.GoToGlobalPositionResponse()

    def SetVelocity(self, request, context):
        self.vehicle.set_velocity_target(LVector3f(request.velocity.x_vel, request.velocity.y_vel, request.velocity.z_vel), body_aligned=(request.frame <= 1))
        self.vehicle.set_pose_target(LVector3f(request.velocity.angular_vel, 0, 0), PoseMode.VELOCITY)
        return control_proto.SetVelocityResponse()

    def SetGimbalPose(self, request, context):
        if request.pose_mode == 0:
            self.vehicle.set_pose_target(LVector3f(
                    request.pose.yaw,
                    max(-90, min(90, request.pose.pitch)),
                    request.pose.roll
                    ), PoseMode.ANGLE
                    )
        elif request.pose_mode == 1:
            self.vehicle.set_pose_target(LVector3f(
                    request.pose.yaw,
                    request.pose.pitch,
                    request.pose.roll
                    ),
                    PoseMode.OFFSET
                    )
        else:
            self.vehicle.set_pose_target(LVector3f(
                    request.pose.yaw,
                    request.pose.pitch,
                    request.pose.roll
                    ), PoseMode.VELOCITY
                    )

        return control_proto.SetGimbalPoseResponse()

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
        gps_info = telemetry_proto.GPSInfo(satellites=15)

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
        gimbals = []
        gid = 0
        gimbal_pose_body = common_proto.Pose()
        gimbal_pose_body.yaw = pose_body.x
        gimbal_pose_body.pitch = pose_body.y
        gimbal_pose_body.roll = pose_body.z
        gimbal_pose = common_proto.Pose()
        gimbal_pose.yaw = pose.x
        gimbal_pose.pitch = pose.y
        gimbal_pose.roll = pose.z
        gimbal_velocity_body = common_proto.Pose()
        gimbal_velocity_body.yaw = 0.0
        gimbal_velocity_body.pitch = angular_vel.y
        gimbal_velocity_body.roll = angular_vel.z
        gimbal_velocity_neu = common_proto.Pose()
        gimbal_velocity_neu.yaw = angular_vel.x
        gimbal_velocity_neu.pitch = angular_vel.y
        gimbal_velocity_neu.roll = angular_vel.z
        gimbal_status = telemetry_proto.GimbalStatus(
                id=gid,
                pose_body=gimbal_pose_body,
                pose_neu=gimbal_pose,
                angular_velocity_body=gimbal_velocity_body,
                angular_velocity_neu=gimbal_velocity_neu,
                )
        gimbals.append(gimbal_status)
        gimbal_info = telemetry_proto.GimbalInfo(gimbals=gimbals)

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
            # Get frame data
            h_res = self.vehicle.camera.texture.get_x_size()
            v_res = self.vehicle.camera.texture.get_y_size()
            data = self.vehicle.camera.texture.getRamImage()
            arr = np.frombuffer(data, dtype=np.uint8)
            arr = arr.reshape((v_res, h_res, 3))
            arr = np.flipud(arr)
            success, jpeg_data = cv2.imencode('.jpg', arr)
            if success:
                telemetry = self.get_telemetry()
                frame = telemetry_proto.EncodedFrame(
                        timestamp=Timestamp().GetCurrentTime(),
                        encoded_data=jpeg_data.tobytes(),
                        id=frame_id,
                        position_info=telemetry.position_info,
                        gimbal_status=telemetry.gimbal_info.gimbals[0],
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
