"""Sets the main camera's exposure and HDR, as given in the driver's args.

The settings take effect while the drone streams, without restarting the
stream, and stay on the drone until changed. A setting left out of the args
is not touched.
"""
import logging
import re

from olympe.enums.camera import (
    auto_exposure_metering_mode,
    exposure_mode,
    iso_sensitivity,
    shutter_speed as ShutterSpeed,
    state,
)
from olympe.messages.camera import (
    exposure_settings,
    hdr_setting,
    set_exposure_settings,
    set_hdr_setting,
)

logger = logging.getLogger('parrot-anafi/camera')

CAM_ID = 0
# The shutter_speed arg that restores automatic exposure
AUTO = 'auto'
# The manual ISO is unused, since ISO stays automatic with a fixed shutter;
# the command needs one all the same
UNUSED_ISO = iso_sensitivity.iso_100
# The highest ISO automatic exposure may use, the camera's highest
MAX_ISO = iso_sensitivity.iso_3200
# A shutter speed as written in the args: "1", or 1 over a number, like "1/500"
# or "1/1.5"
SHUTTER_SPEED_PATTERN = re.compile(r'1(?:/(\d+(?:\.\d+)?))?')
# Seconds to wait for the drone to report a setting taken
REPLY_TIMEOUT_S = 5


class CameraConfigError(RuntimeError):
    """The drone did not take a camera setting."""


def parse_shutter_speed(text):
    """The shutter speed written as a fraction of a second, like "1/500",
    or "1" for a whole second."""
    match = SHUTTER_SPEED_PATTERN.fullmatch(text)
    if match:
        denominator = match[1]
        name = f'shutter_1_over_{denominator.replace(".", "_")}' if denominator else 'shutter_1'
        try:
            return ShutterSpeed[name]
        except KeyError:
            pass
    speeds = ', '.join(speed.name.removeprefix('shutter_').replace('_over_', '/').replace('_', '.')
                       for speed in ShutterSpeed)
    raise ValueError(f'unknown shutter speed {text!r}; the camera has {speeds}')


def configure_camera(drone, shutter_speed=None, hdr=None):
    """Sets the exposure to `shutter_speed` ("1/500", or AUTO), and HDR on or
    off, waiting for the drone to take each one. Raises CameraConfigError if
    it does not, as when the speed is not one the current camera mode
    allows."""
    if shutter_speed is not None:
        if shutter_speed == AUTO:
            mode, speed = exposure_mode.automatic, ShutterSpeed.shutter_1_over_500
        else:
            mode, speed = exposure_mode.manual_shutter_speed, parse_shutter_speed(shutter_speed)
        _send(drone, f'shutter speed {shutter_speed}',
              set_exposure_settings(
                  cam_id=CAM_ID,
                  mode=mode,
                  shutter_speed=speed,
                  iso_sensitivity=UNUSED_ISO,
                  max_iso_sensitivity=MAX_ISO,
                  metering_mode=auto_exposure_metering_mode.standard,
              )
              >> exposure_settings(cam_id=CAM_ID, mode=mode, _policy='wait'))
    if hdr is not None:
        value = state.active if hdr else state.inactive
        _send(drone, f'HDR {value.name}',
              set_hdr_setting(cam_id=CAM_ID, value=value)
              >> hdr_setting(cam_id=CAM_ID, value=value, _policy='wait'))


def _send(drone, setting, expectation):
    if not drone(expectation).wait(REPLY_TIMEOUT_S).success():
        raise CameraConfigError(f'the drone did not take {setting}')
    logger.info(f'camera set to {setting}')
