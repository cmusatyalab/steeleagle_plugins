import os
from functools import wraps
from math import degrees, radians, pi
import time
import logging
from numpy import clip
import grpc
# Protocol imports
import steeleagle_protocol.v1.common.common_pb2 as common_proto
import steeleagle_protocol.v1.messages.telemetry.telemetry_pb2 as telemetry_proto
import steeleagle_protocol.v1.services.driver.control_pb2 as control_proto
from steeleagle_protocol.v1.services.driver.control_pb2_grpc import ControlServiceServicer
from steeleagle_protocol.v1.services.driver.stream_pb2_grpc import StreamServiceServicer
from steeleagle_protocol.v1.services.driver.calibrate_pb2_grpc import CalibrateServiceServicer
# Olympe imports
from olympe.messages.ardrone3.Piloting import TakeOff, Landing, PCMD, Emergency
from olympe.messages.move import extended_move_to, extended_move_by
import olympe.enums.move as move_mode
from olympe.messages.rth import (
    abort,
    return_to_home,
    set_min_altitude,
    set_ending_behavior,
    set_ending_hovering_altitude,
)
import olympe.enums.rth as rth_state
from olympe.enums.ardrone3.PilotingState import FlyingStateChanged_State
from olympe.messages.ardrone3.PilotingState import AttitudeChanged, GpsLocationChanged, AltitudeChanged
from olympe.messages.ardrone3.PilotingState import FlyingStateChanged
from olympe.messages.gimbal import set_target, attitude, max_speed
# Driver imports
from parrot_anafi.velocity_pid import VelocityPIDThread
from parrot_anafi.util import rotate_body_to_neu, rotate_neu_to_body

logger = logging.getLogger('parrot-anafi/control')

# Default speed in meters per second
DEFAULT_SPEED = 3.0
# Default angular speed in degrees per second
DEFAULT_ANGULAR_SPEED = 90.0
DEFAULT_GIMBAL_ID = 0

def setpoint(func):
    """Setpoint decorator.

    Automatically marks the setpoint for the decorated RPC method.
    """
    @wraps(func)
    def wrapper(self, request, context):
        resp = func(self, request, context)
        self.drone.mark_setpoint(resp.setpoint)
        return resp
    return wrapper

def nosetpoint(func):
    """No setpoint decorator.

    Clears the setpoint, if any is current set.
    """
    @wraps(func)
    def wrapper(self, request, context):
        self.drone.mark_setpoint(None)
        return func(self, request, context)
    return wrapper

class Control(ControlServiceServicer):
    """Control Service implementation.
    """

    def __init__(self, drone):
        self.drone = drone
        self.velocity_task = VelocityPIDThread(self.drone)
        self.velocity_task.start()

    def set_flight_mode(self, mode: telemetry_proto.Mode, on_velocity=False):
        """Sets flight mode and toggles velocity control.

        Monitors each flight mode change and if velocity control should
        be enabled, start the velocity task.
        """
        self.drone.set_flight_mode(mode)
        if on_velocity:
            self.velocity_task.resume()
        else:
            self.velocity_task.pause()

    @nosetpoint
    def TakeOff(self, request, context):
        self.set_flight_mode(telemetry_proto.Mode.MODE_TAKEOFF)
        if request.altitude:
            # TODO: spawn off a thread that does this!
            logger.warning('no support for field take_off_altitude, ignoring')
        if self.drone.get_state(FlyingStateChanged)['state'] != FlyingStateChanged_State.landed:
            logger.error('takeoff attempted when drone not landed')
            context.abort(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    'takeoff attemped when drone not landed',
                    )
        self.drone(TakeOff()).wait().success()
        return control_proto.TakeOffResponse(
            expected_mode=telemetry_proto.Mode.MODE_TAKEOFF,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_HOLDING,
        )

    @nosetpoint
    def Land(self, request, context):
        self.set_flight_mode(telemetry_proto.Mode.MODE_LAND)
        if self.drone.get_state(FlyingStateChanged)['state'] == FlyingStateChanged_State.landed:
            logger.error('land attempted when drone not in the air')
            context.abort(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    'land attempted when drone not in the air',
                    )
        self.drone(Landing()).wait().success()
        return control_proto.LandResponse(
            expected_mode=telemetry_proto.Mode.MODE_LAND,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_STOPPED,
        )

    @nosetpoint
    def Hold(self, request, context):
        self.set_flight_mode(telemetry_proto.Mode.MODE_LOITER)
        # Abort an in-progress RTH since PCMD does not overwrite it
        self.drone(abort())
        # Set a slight positive throttle to cancel landing
        self.drone(PCMD(1, 0, 0, 0, 1, timestampAndSeqNum=0)).wait().success()
        return control_proto.HoldResponse(
            expected_mode=telemetry_proto.Mode.MODE_LOITER,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_HOLDING,
        )

    @nosetpoint
    def Kill(self, request, context):
        self.set_flight_mode(telemetry_proto.Mode.MODE_EMERGENCY)
        self.drone(Emergency()).wait().success()
        return control_proto.KillResponse(
            expected_mode=telemetry_proto.Mode.MODE_EMERGENCY,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_STOPPED,
        )

    @nosetpoint
    def ReturnToHome(self, request, context):
        self.set_flight_mode(telemetry_proto.Mode.MODE_RETURN_TO_HOME)
        # Set the end behavior for the RTH
        if request.end_behavior <= 1:
            self.drone(set_ending_behavior(rth_state.ending_behavior.hovering)).wait().success()
            expected_mode = telemetry_proto.Mode.MODE_LOITER
            expected_status = telemetry_proto.MotionStatus.MOTION_STATUS_HOLDING
        else:
            self.drone(set_ending_behavior(rth_state.ending_behavior.landing)).wait().success()
            expected_mode = telemetry_proto.Mode.MODE_LAND
            expected_status = telemetry_proto.MotionStatus.MOTION_STATUS_STOPPED
        # Set the return minimum altitude
        if request.min_return_altitude:
            self.drone(set_min_altitude(request.min_return_altitude)).wait().success()
        # Set the final hovering altitude
        if request.final_altitude:
            self.drone(set_ending_hovering_altitude(request.final_altitude)).wait().success()
        self.drone(return_to_home()).wait().success()
        return control_proto.ReturnToHomeResponse(
            expected_mode=expected_mode,
            expected_status=expected_status,
        )

    @setpoint
    def SetRelativePositionTarget(self, request, context):
        self.set_flight_mode(telemetry_proto.Mode.MODE_GUIDED)
        yaw = self.drone.get_state(AttitudeChanged)['yaw']
        if request.frame <= 1: # Body-aligned
            self.drone(extended_move_by(
                request.position.x,
                request.position.y,
                -request.position.z,
                # RelativePosition.angle is degrees, extended_move_by's d_psi
                # is radians.
                radians(request.position.angle),
                request.speed if request.speed else DEFAULT_SPEED,
                request.speed if request.speed else DEFAULT_SPEED,
                request.angular_speed if request.angular_speed else DEFAULT_ANGULAR_SPEED,
                ) >> FlyingStateChanged(state='flying')
            )
            # Convert the body-frame target into NEU for the reported setpoint
            north, east = rotate_body_to_neu(request.position.x, request.position.y, yaw)
            setpoint = common_proto.RelativePosition(
                x=north,
                y=east,
                z=request.position.z,
                angle=degrees(yaw) + request.position.angle,
            )
        else: # NEU-aligned
            dx, dy = rotate_neu_to_body(request.position.x, request.position.y, yaw)
            heading_delta = (radians(request.position.angle) - yaw + pi) % (2 * pi) - pi
            self.drone(extended_move_by(
                dx, dy,
                -request.position.z,
                heading_delta,
                request.speed if request.speed else DEFAULT_SPEED,
                request.speed if request.speed else DEFAULT_SPEED,
                request.angular_speed if request.angular_speed else DEFAULT_ANGULAR_SPEED,
                ) >> FlyingStateChanged(state='flying')
            )
            setpoint = request.position
        return control_proto.SetRelativePositionTargetResponse(
            setpoint=setpoint,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_HOLDING,
        )

    @setpoint
    def SetGlobalPositionTarget(self, request, context):
        self.set_flight_mode(telemetry_proto.Mode.MODE_GUIDED)
        # Set heading mode
        heading_mode = move_mode.orientation_mode.to_target
        if request.heading_mode > 1:
            heading_mode = move_mode.orientation_mode.heading_start
        gps_altitude = self.drone.get_state(GpsLocationChanged)['altitude']
        relative_altitude = self.drone.get_state(AltitudeChanged)['altitude']
        if request.altitude_mode <= 1: # Relative altitude
            move_altitude = request.position.altitude
            setpoint_altitude = request.position.altitude - relative_altitude + gps_altitude
        else: # Absolute altitude
            move_altitude = request.position.altitude - gps_altitude + relative_altitude
            setpoint_altitude = request.position.altitude
        self.drone(extended_move_to(
            request.position.latitude,
            request.position.longitude,
            move_altitude,
            heading_mode,
            request.position.heading,
            request.speed if request.speed else DEFAULT_SPEED,
            request.speed if request.speed else DEFAULT_SPEED,
            request.angular_speed if request.angular_speed else DEFAULT_ANGULAR_SPEED,
            ) >> FlyingStateChanged(state='flying')
        )
        # heading is intentionally omitted from the setpoint in TO_TARGET
        # mode so it must be set according to the TO_TARGET heading
        setpoint = common_proto.GlobalPosition(
            latitude=request.position.latitude,
            longitude=request.position.longitude,
            altitude=setpoint_altitude,
        )
        if request.heading_mode > 1: # HEADING_START
            setpoint.heading = request.position.heading
        return control_proto.SetGlobalPositionTargetResponse(
            setpoint=setpoint,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_HOLDING,
        )

    @setpoint
    def SetVelocityTarget(self, request, context):
        self.set_flight_mode(telemetry_proto.Mode.MODE_GUIDED, on_velocity=True)
        self.velocity_task.set_target(request.velocity, request.frame)
        # angular_vel is intentionally omitted from the setpoint: telemetry
        # never reports a measured angular velocity, so there's nothing to
        # compare a commanded angular_vel setpoint against.
        if request.frame <= 1: # Body-aligned
            # Convert the body-frame target into NEU for the reported setpoint
            yaw = self.drone.get_state(AttitudeChanged)['yaw']
            north_vel, east_vel = rotate_body_to_neu(request.velocity.x_vel, request.velocity.y_vel, yaw)
            setpoint = common_proto.Velocity(
                x_vel=north_vel,
                y_vel=east_vel,
                z_vel=request.velocity.z_vel,
            )
        else: # NEU-aligned
            setpoint = common_proto.Velocity(
                x_vel=request.velocity.x_vel,
                y_vel=request.velocity.y_vel,
                z_vel=request.velocity.z_vel,
            )
        return control_proto.SetVelocityTargetResponse(
            setpoint=setpoint,
            expected_status=telemetry_proto.MotionStatus.MOTION_STATUS_IN_TRANSIT,
        )

    @setpoint
    def SetGimbalAngleTarget(self, request, context):
        yaw = request.pose.yaw
        pitch = request.pose.pitch
        roll = request.pose.roll
        # Pitch/roll are always horizon-stabilized (absolute); only yaw can be
        # body- or NEU-referenced.
        yaw_frame = 'relative' if request.frame <= 1 else 'absolute'
        if request.angle_mode == control_proto.AngleMode.ANGLE_MODE_OFFSET:
            gimbal_pose = self.drone.get_state(attitude)[DEFAULT_GIMBAL_ID]
            target_yaw = gimbal_pose[f'yaw_{yaw_frame}'] + yaw
            target_pitch = gimbal_pose['pitch_absolute'] + pitch
            target_roll = gimbal_pose['roll_absolute'] + roll
        else:
            target_yaw, target_pitch, target_roll = yaw, pitch, roll
        self.drone(set_target(
            gimbal_id=DEFAULT_GIMBAL_ID,
            control_mode='position',
            yaw_frame_of_reference=yaw_frame if request.pose.HasField('yaw') else 'none',
            yaw=target_yaw,
            pitch_frame_of_reference='absolute' if request.pose.HasField('pitch') else 'none',
            pitch=target_pitch,
            roll_frame_of_reference='absolute' if request.pose.HasField('roll') else 'none',
            roll=target_roll,
            )
        ).wait().success()
        return control_proto.SetGimbalAngleTargetResponse(
            setpoint=common_proto.Pose(
                pitch=target_pitch,
                roll=target_roll,
                yaw=target_yaw,
            ),
        )

    @staticmethod
    def _gimbal_velocity_ratio(vel, axis_max_speed):
        """Converts a deg/s rate into -1..1 ratio of the axis's max speed.

        A max speed of 0 means this gimbal doesn't support rate control on that
        axis. We report it as not-actuated rather than dividing by zero."""
        if axis_max_speed == 0:
            return None
        return clip(vel / axis_max_speed, -1.0, 1.0)

    @setpoint
    def SetGimbalVelocityTarget(self, request, context):
        yaw_vel = request.pose_velocity.yaw_vel
        pitch_vel = request.pose_velocity.pitch_vel
        roll_vel = request.pose_velocity.roll_vel
        frame = 'relative'
        gimbal_max_speed = self.drone.get_state(max_speed)[DEFAULT_GIMBAL_ID]
        yaw_ratio = self._gimbal_velocity_ratio(yaw_vel, gimbal_max_speed['current_yaw'])
        pitch_ratio = self._gimbal_velocity_ratio(pitch_vel, gimbal_max_speed['current_pitch'])
        roll_ratio = self._gimbal_velocity_ratio(roll_vel, gimbal_max_speed['current_roll'])
        self.drone(set_target(
            gimbal_id=DEFAULT_GIMBAL_ID,
            control_mode='velocity',
            yaw_frame_of_reference=frame if yaw_ratio is not None else 'none',
            yaw=yaw_ratio or 0.0,
            pitch_frame_of_reference=frame if pitch_ratio is not None else 'none',
            pitch=pitch_ratio or 0.0,
            roll_frame_of_reference=frame if roll_ratio is not None else 'none',
            roll=roll_ratio or 0.0,
            )
        ).wait().success()
        return control_proto.SetGimbalVelocityTargetResponse(setpoint=request.pose_velocity)
