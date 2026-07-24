import os
from math import sin, cos
import time
import logging
from numpy import clip
from enum import Enum
import grpc
# Protocol imports
import steeleagle_protocol.v1.common_pb2 as common_proto
import steeleagle_protocol.v1.services.driver.control_pb2 as control_proto
from steeleagle_protocol.v1.services.driver.control_pb2_grpc import ControlServiceServicer
from steeleagle_protocol.v1.services.driver.stream_pb2_grpc import StreamServiceServicer
from steeleagle_protocol.v1.services.driver.calibrate_pb2_grpc import CalibrateServiceServicer
# Olympe imports
from olympe.messages.ardrone3.Piloting import TakeOff, Landing, PCMD
from olympe.messages.move import extended_move_to, extended_move_by
import olympe.enums.move as move_mode
from olympe.messages.rth import (
    abort,
    set_custom_location,
    return_to_home,
    set_min_altitude,
    set_ending_behavior,
    set_ending_hovering_altitude,
)
import olympe.enums.rth as rth_state
from olympe.messages.ardrone3.PilotingState import AttitudeChanged, GpsLocationChanged, AltitudeChanged
from olympe.messages.gimbal import set_target, attitude, max_speed
# Driver imports
from parrot_anafi.velocity_pid import VelocityPIDThread

logger = logging.getLogger('parrot-anafi/control')

# Default speed in meters per second
DEFAULT_SPEED = 3.0
# Default angular speed in degrees per second
DEFAULT_ANGULAR_SPEED = 90.0

class Control(ControlServiceServicer):
    """Control Service implementation.
    """
    class FlightMode(Enum):
        LOITER = 'LOITER'
        TAKEOFF_LAND = 'TAKEOFF_LAND'
        VELOCITY = 'VELOCITY'
        GUIDED = 'GUIDED'

    def __init__(self, drone):
        self.drone = drone
        self.mode = Control.FlightMode.LOITER
        self.velocity_task = VelocityPIDThread(self.drone)
        self.velocity_task.start()

    def set_flight_mode(self, mode: FlightMode):
        """Set the internal flight mode of the drone.

        This method tracks an implied flight mode for the drone since Olympe
        does not provide one. Tracking the flight mode prevents the
        VelocityPIDThread from interrupting other flight commands.
        """
        logger.debug(f'switching flight mode to {mode.name}')
        if mode == self.mode:
            return
        if self.mode == Control.FlightMode.VELOCITY:
            # Switching out of velocity mode
            self.velocity_task.pause()
        elif mode == Control.FlightMode.VELOCITY:
            # Switching into velocity mode
            self.velocity_task.resume()
        self.mode = mode

    def TakeOff(self, request, context):
        self.set_flight_mode(Control.FlightMode.TAKEOFF_LAND)
        if request.take_off_altitude:
            logger.warning('no support for field take_off_altitude, ignoring')
        self.drone(TakeOff()).wait().success()
        return control_proto.TakeOffResponse()

    def Land(self, request, context):
        self.set_flight_mode(Control.FlightMode.TAKEOFF_LAND)
        self.drone(Landing()).wait().success()
        return control_proto.LandResponse()

    def Hold(self, request, context):
        self.set_flight_mode(Control.FlightMode.LOITER)
        # Abort an in-progress RTH since PCMD does not overwrite it
        self.drone(abort())
        # Set a slight positive throttle to cancel landing
        self.drone(PCMD(1, 0, 0, 0, 1, timestampAndSeqNum=0)).wait().success()
        return control_proto.HoldResponse()

    def SetHome(self, request, context):
        lat = request.new_home.latitude
        lon = request.new_home.longitude
        alt = request.new_home.altitude
        self.drone(set_custom_location(lat, lon, alt)).wait().success()
        return control_proto.SetHomeResponse()

    def ReturnToHome(self, request, context):
        self.set_flight_mode(Control.FlightMode.GUIDED)
        # Set the end behavior for the RTH
        if request.end_behavior <= 1:
            self.drone(set_ending_behavior(rth_state.ending_behavior.hovering)).wait().success()
        else:
            self.drone(set_ending_behavior(rth_state.ending_behavior.landing)).wait().success()
        # Set the return minimum altitude
        if request.return_altitude:
            self.drone(set_min_altitude(request.return_altitude)).wait().success()
        # Set the final hovering altitude
        if request.final_altitude:
            self.drone(set_ending_hovering_altitude(request.final_altitude)).wait().success()
        self.drone(return_to_home()).wait().success()
        return control_proto.ReturnToHomeResponse()

    def GoToRelativePosition(self, request, context):
        self.set_flight_mode(Control.FlightMode.GUIDED)
        if request.frame <= 1: # Body-aligned
            self.drone(extended_move_by(
                request.position.x,
                request.position.y,
                -request.position.z,
                request.position.angle,
                request.speed if request.speed else DEFAULT_SPEED,
                request.speed if request.speed else DEFAULT_SPEED,
                request.angular_speed if request.angular_speed else DEFAULT_ANGULAR_SPEED,
                )
            )
        else: # NEU-aligned
            psi = self.drone.get_state(AttitudeChanged)['yaw']
            dx = request.position.x * cos(psi) + request.position.y * sin(psi)
            dy = -request.position.x * sin(psi) + request.position.y * cos(psi)
            self.drone(extended_move_by(
                dx, dy,
                -request.position.z,
                request.position.angle,
                request.speed if request.speed else DEFAULT_SPEED,
                request.speed if request.speed else DEFAULT_SPEED,
                request.angular_speed if request.angular_speed else DEFAULT_ANGULAR_SPEED,
                )
            )
        return control_proto.GoToRelativePositionResponse()

    def GoToGlobalPosition(self, request, context):
        self.set_flight_mode(Control.FlightMode.GUIDED)
        # Set heading mode
        heading_mode = move_mode.orientation_mode.to_target
        if request.heading_mode > 1:
            heading_mode = move_mode.orientation_mode.heading_start
        if request.altitude_mode > 1: # Relative altitude
            self.drone(extended_move_to(
                request.position.latitude,
                request.position.longitude,
                request.position.altitude,
                heading_mode,
                request.speed if request.speed else DEFAULT_SPEED,
                request.speed if request.speed else DEFAULT_SPEED,
                request.angular_speed if request.angular_speed else DEFAULT_ANGULAR_SPEED,
                )
            )
        else: # Absolute altitude
            absolute_altitude = request.position.altitude - self.drone.get_state(GpsLocationChanged)['altitude'] \
                    + self.drone.get_state(AltitudeChanged)['altitude']
            self.drone(extended_move_to(
                request.position.latitude,
                request.position.longitude,
                absolute_altitude,
                heading_mode,
                request.speed if request.speed else DEFAULT_SPEED,
                request.speed if request.speed else DEFAULT_SPEED,
                request.angular_speed if request.angular_speed else DEFAULT_ANGULAR_SPEED,
                )
            )
        return control_proto.GoToGlobalPositionResponse()

    def SetVelocity(self, request, context):
        self.velocity_task.set_target(request.velocity, request.frame)
        self.set_flight_mode(Control.FlightMode.VELOCITY)
        return control_proto.SetVelocityResponse()

    def SetGimbalPose(self, request, context):
        yaw = request.pose.yaw
        pitch = request.pose.pitch
        roll = request.pose.roll
        frame = 'relative' if request.frame <= 1 else 'absolute'
        # Actuate the gimbal depending on mode
        if request.pose_mode == control_proto.PoseMode.ANGLE:
            self.drone(set_target(
                gimbal_id=request.gimbal_id,
                control_mode='position',
                yaw_frame_of_reference=frame if yaw else 'none',
                yaw=yaw,
                pitch_frame_of_reference=frame if pitch else 'none',
                pitch=pitch,
                roll_frame_of_reference=frame if roll else 'none',
                roll=roll,
                )
            ).wait().success()
        elif request.pose_mode == control_proto.PoseMode.OFFSET:
            gimbal_pose = self.drone.get_state(attitude)[request.gimbal_id]
            self.drone(set_target(
                gimbal_id=request.gimbal_id,
                control_mode='position',
                yaw_frame_of_reference=frame if yaw else 'none',
                yaw=gimbal_pose[f'yaw_{frame}'] + yaw,
                pitch_frame_of_reference=frame if pitch else 'none',
                pitch=gimbal_pose[f'pitch_{frame}'] + pitch,
                roll_frame_of_reference=frame if roll else 'none',
                roll=gimbal_pose[f'roll_{frame}'] + roll,
                )
            ).wait().success()
        else:
            gimbal_max_speed = self.drone.get_state(max_speed)[request.gimbal_id]
            self.drone(set_target(
                gimbal_id=request.gimbal_id,
                control_mode='velocity',
                yaw_frame_of_reference='relative',
                yaw=clip(yaw / gimbal_max_speed['current_yaw'], -1.0, 1.0),
                pitch_frame_of_reference='relative',
                pitch=clip(pitch / gimbal_max_speed['current_pitch'], -1.0, 1.0),
                roll_frame_of_reference='relative',
                roll=clip(roll / gimbal_max_speed['current_roll'], -1.0, 1.0),
                )
            ).wait().success()
        return control_proto.SetGimbalPoseResponse()
