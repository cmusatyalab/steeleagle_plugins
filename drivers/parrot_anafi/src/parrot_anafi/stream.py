import grpc
import os
import time
import logging
from math import sin, cos, radians, degrees
import numpy as np
import cv2
# Protocol imports
import steeleagle_protocol.v1.common.common_pb2 as common_proto
import steeleagle_protocol.v1.services.driver.stream_pb2 as stream_proto
import steeleagle_protocol.v1.messages.telemetry.telemetry_pb2 as telemetry_proto
from steeleagle_protocol.v1.services.driver.stream_pb2_grpc import StreamServiceServicer
from google.protobuf.timestamp_pb2 import Timestamp
from google.protobuf.any_pb2 import Any
# Olympe imports
from olympe.messages.ardrone3.PilotingState import (
    AttitudeChanged,
    GpsLocationChanged,
    AltitudeChanged,
    SpeedChanged,
    AlertStateChanged,
    HeadingLockedStateChanged,
    FlyingStateChanged,
)
from olympe.enums.ardrone3.PilotingState import AlertStateChanged_State, HeadingLockedStateChanged_State, FlyingStateChanged_State
from olympe.messages.ardrone3.GPSSettingsState import HomeChanged, GPSFixStateChanged
from olympe.messages.ardrone3.GPSState import NumberOfSatelliteChanged
from olympe.messages.common.CommonState import BatteryStateChanged, LinkSignalQuality
from olympe.messages.gimbal import attitude

logger = logging.getLogger('parrot-anafi/stream')

# Approximate meters per degree of latitude, used to convert GPS deltas
# into a local ENU frame for relative_position (equirectangular approximation).
METERS_PER_DEGREE_LATITUDE = 111320.0

class Stream(StreamServiceServicer):
    """Stream Service implementation."""
    def __init__(self, drone, ip):
        self.drone = drone
        self.ip = ip
        self.cap = None

    def get_position_info(self) -> telemetry_proto.PositionInfo:
        """Get position info from the drone."""
        try:
            home = self.drone.get_state(HomeChanged)
        except Exception:
            home = {'latitude': 0.0, 'longitude': 0.0, 'altitude': 0.0}
        gps = self.drone.get_state(GpsLocationChanged)
        att = self.drone.get_state(AttitudeChanged)
        speed = self.drone.get_state(SpeedChanged)
        alt = self.drone.get_state(AltitudeChanged)
        state = self.drone.get_state(FlyingStateChanged)

        psi = att['yaw']
        north = (gps['latitude'] - home['latitude']) * METERS_PER_DEGREE_LATITUDE
        east = (gps['longitude'] - home['longitude']) * METERS_PER_DEGREE_LATITUDE * cos(radians(home['latitude']))
        x = north * cos(psi) + east * sin(psi)
        y = -north * sin(psi) + east * cos(psi)

        vx = speed['speedX']
        vy = speed['speedY']
        vz = -speed['speedZ']
        vx_body = speed['speedX'] * cos(psi) + speed['speedY'] * sin(psi)
        vy_body = -speed['speedX'] * sin(psi) + speed['speedY'] * cos(psi)

        motion_status = 0
        if state['state'] == FlyingStateChanged_State.hovering:
            motion_status = 1
        elif state['state'] == FlyingStateChanged_State.landed:
            motion_status = 3
        else:
            motion_status = 2

        setpoint = Any()
        if self.drone.setpoint:
            setpoint.Pack(self.drone.setpoint)

        return telemetry_proto.PositionInfo(
            home=common_proto.GlobalPosition(
                latitude=home['latitude'],
                longitude=home['longitude'],
                altitude=home['altitude'],
            ),
            global_position=common_proto.GlobalPosition(
                latitude=gps['latitude'],
                longitude=gps['longitude'],
                altitude=gps['altitude'],
                heading=degrees(psi) % 360,
            ),
            relative_position=common_proto.RelativePosition(
                x=x, y=y, z=alt['altitude'], angle=psi,
            ),
            velocity_body=common_proto.Velocity(
                x_vel=vx_body, y_vel=vy_body, z_vel=vz,
            ),
            velocity_neu=common_proto.Velocity(
                x_vel=vx, y_vel=vy, z_vel=vz,
            ),
            motion_status=motion_status,
            setpoint=setpoint,
        )

    def get_gimbal_status(self, gimbal_id=0):
        """Get gimbal status for a given gimbal."""
        pose = self.drone.get_state(attitude)[gimbal_id]
        return telemetry_proto.GimbalStatus(
            id=gimbal_id,
            pose_body=common_proto.Pose(
                pitch=pose['pitch_relative'],
                roll=pose['roll_relative'],
                yaw=pose['yaw_relative'],
            ),
            pose_neu=common_proto.Pose(
                pitch=pose['pitch_absolute'],
                roll=pose['roll_absolute'],
                yaw=pose['yaw_absolute'],
            ),
        )

    def get_gimbal_info(self):
        """Get gimbal info from the drone."""
        gimbal_ids = self.drone.get_state(attitude).keys()
        return telemetry_proto.GimbalInfo(
            gimbals=[self.get_gimbal_status(gimbal_id) for gimbal_id in gimbal_ids]
        )

    def get_battery_info(self):
        """Get battery info from the drone."""
        percent = 100
        battery_state = self.drone.get_state(BatteryStateChanged)
        if battery_state:
            percent = battery_state['percent']
        return telemetry_proto.BatteryInfo(percentage=percent)

    def get_satellite_count(self):
        """Get satellite count from the drone."""
        try:
            return self.drone.get_state(NumberOfSatelliteChanged)['numberOfSatellite']
        except Exception:
            return 0

    def get_gps_info(self):
        """Get GPS info from the drone."""
        return telemetry_proto.GPSInfo(satellites=self.get_satellite_count())

    def get_alert_info(self):
        """Get alert info from the drone."""
        try:
            alert_state = self.drone.get_state(AlertStateChanged)['state']
        except Exception:
            alert_state = AlertStateChanged_State.none
        try:
            gps_fixed = self.drone.get_state(GPSFixStateChanged)['fixed']
        except Exception:
            gps_fixed = False
        try:
            link_quality = self.drone.get_state(LinkSignalQuality)['value'] & 0x0F
        except Exception:
            link_quality = 0
        try:
            heading_lock = self.drone.get_state(HeadingLockedStateChanged)['state']
        except Exception:
            heading_lock = HeadingLockedStateChanged_State.critical
        satellites = self.get_satellite_count()

        battery_warning = telemetry_proto.AlertInfo.BatteryWarning.BATTERY_WARNING_UNSPECIFIED
        if alert_state in (AlertStateChanged_State.critical_battery, AlertStateChanged_State.almost_empty_battery):
            battery_warning = telemetry_proto.AlertInfo.BatteryWarning.BATTERY_WARNING_CRITICAL
        elif alert_state == AlertStateChanged_State.low_battery:
            battery_warning = telemetry_proto.AlertInfo.BatteryWarning.BATTERY_WARNING_LOW

        gps_warning = telemetry_proto.AlertInfo.GPSWarning.GPS_WARNING_UNSPECIFIED
        if not gps_fixed:
            gps_warning = telemetry_proto.AlertInfo.GPSWarning.GPS_WARNING_NO_FIX
        elif satellites < 6:
            gps_warning = telemetry_proto.AlertInfo.GPSWarning.GPS_WARNING_WEAK_SIGNAL

        magnetometer_warning = telemetry_proto.AlertInfo.MagnetometerWarning.MAGNETOMETER_WARNING_UNSPECIFIED
        if alert_state in (AlertStateChanged_State.magneto_pertubation, AlertStateChanged_State.magneto_low_earth_field):
            magnetometer_warning = telemetry_proto.AlertInfo.MagnetometerWarning.MAGNETOMETER_WARNING_PERTURBATIONS

        connection_warning = telemetry_proto.AlertInfo.ConnectionWarning.CONNECTION_WARNING_UNSPECIFIED
        if link_quality == 1:
            connection_warning = telemetry_proto.AlertInfo.ConnectionWarning.CONNECTION_WARNING_DISCONNECTED
        elif link_quality == 2:
            connection_warning = telemetry_proto.AlertInfo.ConnectionWarning.CONNECTION_WARNING_WEAK_CONNECTION

        compass_warning = telemetry_proto.AlertInfo.CompassWarning.COMPASS_WARNING_UNSPECIFIED
        if heading_lock == HeadingLockedStateChanged_State.critical:
            compass_warning = telemetry_proto.AlertInfo.CompassWarning.COMPASS_WARNING_NO_LOCK
        elif heading_lock == HeadingLockedStateChanged_State.warning:
            compass_warning = telemetry_proto.AlertInfo.CompassWarning.COMPASS_WARNING_WEAK_LOCK

        return telemetry_proto.AlertInfo(
            battery_warning=battery_warning,
            gps_warning=gps_warning,
            magnetometer_warning=magnetometer_warning,
            connection_warning=connection_warning,
            compass_warning=compass_warning,
        )

    # TODO: GetVideoStreamURL

    def StreamVideoFrames(self, request, context):
        if not self.cap:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;udp"
            self.cap = cv2.VideoCapture(
                    f"rtsp://{self.ip}/live",
                    cv2.CAP_FFMPEG,
                    (cv2.CAP_PROP_N_THREADS, 1),
                )

        framerate = np.clip(request.target_fps, 1, 30) if request.target_fps else 30
        frame_id = 0
        ts = Timestamp()
        while True:
            try:
                ret, cv_frame = self.cap.read()
                if not ret:
                    logger.warning('frame could not be read')
                    time.sleep(1.0 / framerate)
                    continue
                success, encoded_img = cv2.imencode('.jpg', cv_frame)
                if not success:
                    logger.warning('frame could not be decoded')
                    time.sleep(1.0 / framerate)
                    continue
                ts.GetCurrentTime()
                frame = telemetry_proto.EncodedFrame(
                    timestamp=ts,
                    id=frame_id,
                    encoded_data=encoded_img.tobytes(),
                    position_info=self.get_position_info(),
                    gimbal_status=self.get_gimbal_status(),
                )
                frame_id += 1
                yield stream_proto.StreamVideoFramesResponse(frame=frame)
                time.sleep(1.0 / framerate)
            except Exception as e:
                logger.warning(f'frame could not be read, reason: {e}')
                time.sleep(1.0 / framerate)

    def StreamTelemetry(self, request, context):
        framerate = np.clip(request.target_fps, 1, 60) if request.target_fps else 30
        ts = Timestamp()
        while True:
            try:
                ts.GetCurrentTime()
                telemetry = telemetry_proto.Telemetry(
                    timestamp=ts,
                    battery_info=self.get_battery_info(),
                    gps_info=self.get_gps_info(),
                    position_info=self.get_position_info(),
                    gimbal_info=self.get_gimbal_info(),
                    alert_info=self.get_alert_info(),
                )
                yield stream_proto.StreamTelemetryResponse(telemetry=telemetry)
                time.sleep(1.0 / framerate)
            except Exception as e:
                logger.warning(f'telemetry frame could not be generated, reason: {e}')
                time.sleep(1.0 / framerate)
