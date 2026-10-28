"""Estimates where on the ground a detection is, from the vehicle's position
and the gimbal's pose.

The camera is a pinhole: a pixel offset from the image center maps to a ray
through the tangent of its angle, not through a linear fraction of the field of
view. The vehicle is assumed level (its roll and pitch are not in the
telemetry), the gimbal's roll is ignored, and the ground is flat at the
vehicle's start height. Gimbal pitch is 0 when level and negative looking down.
"""

import math
from dataclasses import dataclass

from pyproj import Geod

WGS84 = Geod(ellps="WGS84")
CENTER = 0.5


@dataclass(frozen=True)
class Camera:
    hfov_deg: float
    vfov_deg: float

    def focal_lengths(self):
        """The horizontal and vertical focal lengths, in image widths and
        heights."""
        return (
            CENTER / math.tan(math.radians(self.hfov_deg) / 2),
            CENTER / math.tan(math.radians(self.vfov_deg) / 2),
        )


def estimate_location(box, position_info, gimbal_info, camera):
    """The (lat, lon) the bottom center of `box` (y_min, x_min, y_max, x_max,
    as fractions of the image) is at on the ground; the vehicle's own position
    if the frame has no gimbal pose. None if it cannot be estimated: the ray
    never reaches the ground, or the telemetry lacks what it needs."""
    if not position_info.HasField("global_position"):
        return None
    vehicle = position_info.global_position
    if not (vehicle.HasField("latitude") and vehicle.HasField("longitude")):
        return None
    if gimbal_info is None or not (
        gimbal_info.HasField("pose_body") and gimbal_info.pose_body.HasField("pitch")
    ):
        return vehicle.latitude, vehicle.longitude
    if not vehicle.HasField("heading") or not position_info.relative_position.HasField(
        "z"
    ):
        return None

    y_min, x_min, y_max, x_max = box
    focal_x, focal_y = camera.focal_lengths()
    right = ((x_min + x_max) / 2 - CENTER) / focal_x
    down = (y_max - CENTER) / focal_y

    tilt = math.radians(-gimbal_info.pose_body.pitch)  # degrees below level
    toward_ground = math.sin(tilt) + down * math.cos(tilt)
    if toward_ground <= 0:
        return None
    reach = position_info.relative_position.z / toward_ground
    ahead = reach * (math.cos(tilt) - down * math.sin(tilt))
    aside = reach * right

    azimuth = vehicle.heading + math.degrees(math.atan2(aside, ahead))
    lon, lat, _ = WGS84.fwd(
        vehicle.longitude, vehicle.latitude, azimuth, math.hypot(ahead, aside)
    )
    return lat, lon
