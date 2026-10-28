import grpc
import os
import threading
import time
import logging
from math import sin, cos, radians, degrees
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
# Driver imports
from parrot_anafi.raw_frames import RawFramePublisher
from parrot_anafi.publisher import Publisher

logger = logging.getLogger('parrot-anafi/stream')

# Approximate meters per degree of latitude, used to convert GPS deltas
# into a local ENU frame for relative_position (equirectangular approximation).
METERS_PER_DEGREE_LATITUDE = 111320.0
DEFAULT_GIMBAL_ID = 0
# Seconds to wait before reopening the drone's video stream, after it failed
# to open or stopped giving frames.
CAPTURE_RETRY_DELAY = 1.0
# Failed reads in a row after which the video stream is reopened. A read fails
# at once on a broken stream, or after OpenCV's read timeout on a silent one.
MAX_FAILED_READS = 10
# Name of the drone's camera, matching anafi4k.plugin.toml
CAMERA_NAME = 'rgb'
# Rate at which telemetry is published
TELEMETRY_FPS = 30
# Uriel payload type of telemetry, as named in plugin configs
TELEMETRY_TYPE = 'steeleagle_protocol/v1/messages/telemetry/Telemetry'
# Room in an input for telemetry to grow, e.g. once a setpoint or alert is set
TELEMETRY_HEADROOM = 4 << 10

RESOLUTION_DIMENSIONS = {
    stream_proto.RESOLUTION_360P: (640, 360),
    stream_proto.RESOLUTION_480P: (854, 480),
    stream_proto.RESOLUTION_720P: (1280, 720),
    stream_proto.RESOLUTION_1080P: (1920, 1080),
    stream_proto.RESOLUTION_4K: (3840, 2160),
}

class Stream(StreamServiceServicer):
    """Stream Service implementation."""
    def __init__(self, drone, ip):
        self.drone = drone
        self.ip = ip
        self._capture_lock = threading.Lock()
        self._grabber_thread = None
        # The camera stream the vehicle asked for, as (uri, dimensions), or
        # None. The reading thread publishes it, since the publisher must
        # stay on one thread, so it is only handed over here.
        self._camera_request = None
        self._raw_frames = None
        # Telemetry streams replace those started before them, under this lock
        self._telemetry_lock = threading.Lock()
        self._telemetry_publisher = None

    def _start_capture(self):
        """Starts reading the drone's video stream, once. The stream is opened
        on the reading thread, so a slow drone delays only the frames, not
        the StreamCameras call."""
        with self._capture_lock:
            if self._grabber_thread is not None:
                return
            self._grabber_thread = threading.Thread(target=self._grab_frames, daemon=True)
            self._grabber_thread.start()

    def _open_capture(self):
        """Opens the drone's video stream, waiting for it as long as OpenCV
        does."""
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;udp"
        return cv2.VideoCapture(
                f"rtsp://{self.ip}/live",
                cv2.CAP_FFMPEG,
                (cv2.CAP_PROP_N_THREADS, 1),
            )

    def _grab_frames(self):
        """Reads the drone's video stream for good, reopening it whenever it
        fails to open or stops giving frames. The publisher is used only here,
        on this thread.
        """
        try:
            while True:
                cap = self._open_capture()
                if cap.isOpened():
                    self._read_frames(cap)
                    logger.warning(f'video stream stopped; reopening it in {CAPTURE_RETRY_DELAY} s')
                else:
                    logger.warning(f'video stream could not be opened; retrying in {CAPTURE_RETRY_DELAY} s')
                cap.release()
                time.sleep(CAPTURE_RETRY_DELAY)
        finally:
            if self._raw_frames is not None:
                self._raw_frames.close()

    def _update_publisher(self):
        """Replaces the publisher if the vehicle asked for another camera
        stream since the last frame, or closes it if it asked for none."""
        request = self._camera_request
        current = None
        if self._raw_frames is not None:
            current = (self._raw_frames.uri, self._raw_frames.dimensions)
        if request == current:
            return
        if self._raw_frames is not None:
            self._raw_frames.close()
            self._raw_frames = None
        if request is not None:
            uri, dimensions = request
            self._raw_frames = RawFramePublisher(
                uri, dimensions, lambda: (self.get_position_info(), self.get_gimbal_info()))

    def _read_frames(self, cap):
        """Reads frames from `cap`, and publishes each one to Uriel, if the
        vehicle asked for the camera. Returns once MAX_FAILED_READS reads in
        a row fail.
        """
        failed_reads = 0
        while failed_reads < MAX_FAILED_READS:
            ret, frame = cap.read()
            if not ret:
                failed_reads += 1
                continue
            failed_reads = 0
            self._update_publisher()
            if self._raw_frames is not None:
                try:
                    self._raw_frames.publish(frame)
                except Exception as e:
                    logger.warning(f'raw frame could not be published, reason: {e}')

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

        fix = self.drone.get_state(GPSFixStateChanged)
        gps_valid = fix['fixed'] and gps["latitude"] != 500.0 and gps["longitude"] != 500.0
        if not gps_valid:
            gps = {'latitude': 0.0, 'longitude': 0.0, 'altitude': 0.0}

        psi = att['yaw']
        if gps_valid:
            north = (gps['latitude'] - home['latitude']) * METERS_PER_DEGREE_LATITUDE
            east = (gps['longitude'] - home['longitude']) * METERS_PER_DEGREE_LATITUDE * cos(radians(home['latitude']))
        else:
            north = 0.0
            east = 0.0

        vx = speed['speedX']
        vy = speed['speedY']
        vz = -speed['speedZ']
        vx_body = speed['speedX'] * cos(psi) + speed['speedY'] * sin(psi)
        vy_body = -speed['speedX'] * sin(psi) + speed['speedY'] * cos(psi)

        setpoint = Any()
        if self.drone.setpoint:
            setpoint.Pack(self.drone.setpoint)

        return telemetry_proto.PositionInfo(
            home=common_proto.GlobalPosition(
                latitude=home['latitude'],
                longitude=home['longitude'],
                altitude=home['altitude'],
            ) if gps_valid else None,
            global_position=common_proto.GlobalPosition(
                latitude=gps['latitude'],
                longitude=gps['longitude'],
                altitude=gps['altitude'],
                heading=degrees(psi) % 360,
            ) if gps_valid else None,
            relative_position=common_proto.RelativePosition(
                x=north, y=east, z=alt['altitude'], angle=degrees(psi),
            ),
            velocity_body=common_proto.Velocity(
                x_vel=vx_body, y_vel=vy_body, z_vel=vz,
            ),
            velocity_neu=common_proto.Velocity(
                x_vel=vx, y_vel=vy, z_vel=vz,
            ),
            setpoint=setpoint,
        )

    def get_motion_status(self):
        """Get motion status from the drone."""
        state = self.drone.get_state(FlyingStateChanged)
        if state['state'] == FlyingStateChanged_State.hovering:
            return telemetry_proto.MotionStatus.MOTION_STATUS_HOLDING
        elif state['state'] == FlyingStateChanged_State.landed:
            return telemetry_proto.MotionStatus.MOTION_STATUS_STOPPED
        else:
            return telemetry_proto.MotionStatus.MOTION_STATUS_IN_TRANSIT

    def get_mode(self):
        """Get the mode of the drone."""
        return self.drone.flight_mode

    def get_gimbal_info(self, gimbal_id=DEFAULT_GIMBAL_ID):
        """Get gimbal info for the primary gimbal."""
        pose = self.drone.get_state(attitude)[gimbal_id]

        gimbal_setpoint = Any()
        if self.drone.setpoint:
            gimbal_setpoint.Pack(self.drone.setpoint)

        return telemetry_proto.GimbalInfo(
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
            gimbal_setpoint=gimbal_setpoint,
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
        return telemetry_proto.GpsInfo(satellites=self.get_satellite_count())

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

        gps_warning = telemetry_proto.AlertInfo.GpsWarning.GPS_WARNING_UNSPECIFIED
        if not gps_fixed:
            gps_warning = telemetry_proto.AlertInfo.GpsWarning.GPS_WARNING_NO_FIX
        elif satellites < 6:
            gps_warning = telemetry_proto.AlertInfo.GpsWarning.GPS_WARNING_WEAK_SIGNAL

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

    def get_telemetry(self):
        """Get telemetry from the drone."""
        ts = Timestamp()
        ts.GetCurrentTime()
        return telemetry_proto.Telemetry(
            timestamp=ts,
            battery_info=self.get_battery_info(),
            gps_info=self.get_gps_info(),
            position_info=self.get_position_info(),
            gimbal_info=self.get_gimbal_info(),
            alert_info=self.get_alert_info(),
            motion_status=self.get_motion_status(),
            mode=self.get_mode(),
        )

    def StreamCameras(self, request, context):
        # The drone has one camera, so it is streamed if it is requested, and
        # stopped otherwise. Any other camera is an error.
        requests = list(request.secondaries)
        if request.HasField('primary'):
            requests.append(request.primary)
        unknown = [r.id for r in requests if r.id != CAMERA_NAME]
        if unknown:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f'unknown cameras {unknown}, only {CAMERA_NAME!r} exists',
            )
        if not requests:
            self._camera_request = None
            return stream_proto.StreamCamerasResponse()
        camera = requests[0]
        dimensions = None
        if camera.HasField('target_resolution'):
            dimensions = RESOLUTION_DIMENSIONS.get(camera.target_resolution)
        self._camera_request = (camera.uri, dimensions)
        self._start_capture()
        return stream_proto.StreamCamerasResponse()

    def StreamTelemetry(self, request, context):
        if not request.uri:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, 'no telemetry uri given')
        with self._telemetry_lock:
            if self._telemetry_publisher is not None:
                self._telemetry_publisher.stop()
            self._telemetry_publisher = Publisher(
                request.uri,
                TELEMETRY_TYPE,
                TELEMETRY_FPS,
                lambda: self.get_telemetry().SerializeToString(),
                headroom=TELEMETRY_HEADROOM,
            )
        return stream_proto.StreamTelemetryResponse()
