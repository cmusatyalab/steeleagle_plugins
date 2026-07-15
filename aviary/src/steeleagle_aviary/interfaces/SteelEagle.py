import time
import grpc
import threading
from concurrent import futures
import numpy as np
import zmq
import zmq.asyncio
from math import sin, cos
from google.protobuf.json_format import ParseDict
# Panda3d imports
from panda3d.core import LVector3f, LPoint3f
# Utility imports
from util import calculate_bearing
from datatypes import GeodeticPoint, Mode, PoseMode
# Simulator imports
from vehicle import Vehicle
from engines.base import EngineHolder
# Protocol import
import steeleagle_protocol.v1.services.driver.control_pb2 as control_proto
from steeleagle_protocol.v1.services.driver.control_pb2_grpc import ControlServiceServicer, add_ControlServiceServicer_to_server

class SteelEagle:
    """Control interface wrapper."""

    def __init__(self,
                 vehicle: Vehicle,
                 address: str,
                 **kwargs
                 ):
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))

        add_ControlServicer_to_server(Control(vehicle), self.server)
        self.server.add_insecure_port(address)
        self.server.start()

class Control(ControlServicer):
    """gRPC control interface."""

    def __init__(self, vehicle: Vehicle):
        self.vehicle = vehicle

    def TakeOff(self, request, context):
        if self.vehicle.current_position().z <= 0.0:
            self.vehicle.set_position_target(self.vehicle.current_position() + LVector3f(0, 0, request.take_off_altitude))
            return control_proto.TakeOffResponse()
        context.abort(grpc.StatusCode.INTERNAL, "TakeOff cannot be called on vehicle in the air")

    def Land(self, request, context):
        current = self.vehicle.current_position()
        self.vehicle.set_position_target(LPoint3f(current.x, current.y, self.vehicle.sim_origin.z))
        return control_proto.LandResponse()

    def Hold(self, request, context):
        self.vehicle.set_joystick_target(LVector3f(0, 0, 0))
        self.vehicle.set_pose_target(LVector3f(0, 0, 0), PoseMode.VELOCITY)
        return control_proto.HoldResponse()

    def SetHome(self, request, context):
        self.vehicle.origin.latitude = request.location.latitude
        self.vehicle.origin.longitude = request.location.longitude
        return control_proto.SetHomeResponse()

    def ReturnToHome(self, request, context):
        self.vehicle.set_position_target(self.vehicle.sim_origin)
        return control_proto.ReturnToHomeResponse()

    def GoToRelativePosition(self, request, context):
        offset = LVector3f(request.x, request.y, request.z)
        position = self.vehicle.current_position()
        pose = self.vehicle.current_pose()
        heading_aligned = request.frame <= 1
        new_position = calculate_position_from_offset(position, pose, offset, heading_aligned=heading_aligned)
        new_pose = LPoint3f(pose.x + request.angle, pose.y, pose.z)
        self.vehicle.set_position_target(new_position)
        self.vehicle.set_pose_target(new_pose)
        return control_proto.GoToRelativePositionResponse()

    def GoToGlobalPosition(self, request, context):
        # If absolute altitude is specified, add the target altitude
        # to the current altitude
        if request.altitude_mode <= 1:
            altitude = request.location.altitude - self.vehicle.sim_origin.z
        else:
            position = self.vehicle.current_position()
            altitude = position.z + request.location.altitude

        self.vehicle.set_position_target(self.vehicle.convert_to_sim(
                GeodeticPoint(
                    request.location.latitude,
                    request.location.longitude,
                    altitude
                    )
                ))

        # If heading mode is TO_TARGET, set the pose to look at the
        # position target
        if request.heading_mode <= 1:
            position = self.vehicle.current_geodetic_position()
            bearing = calculate_bearing(
                    position.latitude,
                    position.longitude,
                    request.location.latitude,
                    request.location.longitude
                    )
            self.vehicle.set_pose_target(LVector3f(bearing, 0, 0), PoseMode.ANGLE)
        
        return control_proto.GoToGlobalPositionResponse()
    
    def SetVelocity(self, request, context):
        self.vehicle.set_joystick_target(LVector3f(request.velocity.x_vel, request.velocity.y_vel, request.velocity.z_vel))
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
                    max(-90, min(90, request.pose.pitch)),
                    request.pose.roll
                    ) + self.vehicle.current_pose(),
                    PoseMode.ANGLE
                    )
        else:
            self.vehicle.set_pose_target(LVector3f(
                    request.pose.yaw,
                    request.pose.pitch,
                    request.pose.roll
                    ), PoseMode.VELOCITY
                    )

        return control_proto.SetGimbalPoseResponse()

    """ Getter methods """
    def get_vehicle_info(self):
        vehicle_info = telemetry_protocol.VehicleInfo()
        vehicle_info.name = self.vehicle.name
        vehicle_info.model = 'Simulation'
        vehicle_info.manufacturer = 'SteelEagle'
        if self.vehicle.current_position().z == 0:
            vehicle_info.motion_status = 0
        elif self.vehicle.position_reached() and self.vehicle.pose_reached():
            vehicle_info.motion_status = 2
        else:
            vehicle_info.motion_status = 3
        vehicle_info.battery_info.percentage = 100
        vehicle_info.gps_info.satellites = 13
        return vehicle_info

    def get_position_info(self):
        # Position Info
        pos_info = telemetry_protocol.PositionInfo()
        home_pos = common_protocol.Location()
        home_pos.latitude = self.vehicle.origin.latitude
        home_pos.longitude = self.vehicle.origin.longitude
        home_pos.altitude = self.vehicle.origin.altitude
        pos_info.home.CopyFrom(home_pos)
        # Global position
        veh_global = self.vehicle.current_geodetic_position()
        pos_info.global_position.latitude = veh_global.latitude
        pos_info.global_position.longitude = veh_global.longitude
        pos_info.global_position.altitude = veh_global.altitude
        pos_info.global_position.heading = self.vehicle.current_pose().x
        # Relative position
        veh_rel = self.vehicle.current_position()
        pos_info.relative_position.x = veh_rel.x
        pos_info.relative_position.y = veh_rel.y
        pos_info.relative_position.z = veh_rel.z - self.vehicle.sim_origin.z
        # Velocity
        vel = common_protocol.Velocity()
        velocity = self.vehicle.get_velocity(1.0)
        ang_velocity = self.vehicle.get_angular_velocity(1.0)
        vel.x_vel = velocity.x
        vel.y_vel = velocity.y
        vel.z_vel = velocity.z
        vel.angular_vel = ang_velocity.x
        pos_info.velocity_neu.CopyFrom(vel)
        # TODO: Body velocity
        body_velocity = self.vehicle.get_velocity_body(1.0)
        vel.x_vel = body_velocity.x
        vel.y_vel = body_velocity.y
        vel.z_vel = body_velocity.z
        pos_info.velocity_body.CopyFrom(vel)
        # TODO: Setpoint
        return pos_info

    def get_gimbal_info(self):
        gimbal_info = telemetry_protocol.GimbalInfo()
        gimbal_info.num_gimbals = 1
        gimbal_status = telemetry_protocol.GimbalStatus()
        pose_body = self.vehicle.current_pose()
        gimbal_status.pose_neu.yaw = pose_body.x
        gimbal_status.pose_neu.pitch = pose_body.y
        gimbal_status.pose_neu.roll = pose_body.z
        gimbal_info.gimbals.append(gimbal_status)
        return gimbal_info
