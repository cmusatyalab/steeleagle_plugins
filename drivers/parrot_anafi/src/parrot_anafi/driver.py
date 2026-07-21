import os
import math
import time
import logging
from enum import Enum
import grpc
# Protocol imports
import steeleagle_protocol.v1.common_pb2 as common_proto
import steeleagle_protocol.v1.messages.stream_pb2 as telemetry_proto
import steeleagle_protocol.v1.services.control_pb2 as control_proto
import steeleagle_protocol.v1.services.stream_pb2 as control_proto
import steeleagle_protocol.v1.services.calibrate_pb2 as control_proto
from steeleagle_protocol.v1.services.control_pb2_grpc import ControlServiceServicer
from steeleagle_protocol.v1.services.stream_pb2_grpc import StreamServiceServicer
from steeleagle_protocol.v1.services.calibrate_pb2_grpc import CalibrateServiceServicer
# Olympe imports
from olympe.messages.ardrone3.Piloting import TakeOff, Landing, PCMD, extended_move_to, extended_move_by
from olympe.messages.rth import abort, set_custom_location, return_to_home, state, takeoff_location, ending_hovering_altitude, ending_behavior
import olympe.enums.rth as rth_state
import olympe.enums.ending_behavior as rth_ending_behavior
from olympe.messages.common.CommonState import BatteryStateChanged
from olympe.messages.ardrone3.SpeedSettingsState import MaxRotationSpeedChanged
from olympe.messages.ardrone3.PilotingState import HeadingLockedStateChanged, AttitudeChanged, GpsLocationChanged, AltitudeChanged, FlyingStateChanged, SpeedChanged, moveToChanged, moveByChanged
from olympe.messages.ardrone3.GPSSettingsState import GPSFixStateChanged
from olympe.messages.ardrone3.GPSState import NumberOfSatelliteChanged
from olympe.messages.alarms import alarms
from olympe.messages.gimbal import set_target, attitude
import olympe.enums.move as move_mode

logger = logging.getLogger('parrot-anafi/driver')

class Control(ControlServiceServicer):
    """Control Service implementation.
    """
    def __init__(self, drone):
        self.drone = drone

    def TakeOff(self, request, context):
        if request.take_off_altitude:
            logger.warning('no support for field take_off_altitude, ignoring')
        self.drone(TakeOff()).wait().success()
        return driver_proto.TakeOffResponse()

    def Land(self, request, context):
        self.drone(Landing()).wait().success()
        return driver_proto.LandResponse()

    def Hold(self, request, context):
        # Abort an in-progress RTH since PCMD does not overwrite it
        self.drone(abort()).wait().success()
        # Set a slight positive throttle to cancel landing
        self.drone(PCMD(1, 0, 0, 0, 1)).wait().success()
        return driver_proto.HoldResponse()

    def SetHome(self, request, context):
        lat = request.new_home.latitude
        lon = request.new_home.longitude
        alt = request.new_home.altitude
        self.drone(set_custom_location(lat, lon, alt)).wait().success()

    def ReturnToHome(self, request, context):



