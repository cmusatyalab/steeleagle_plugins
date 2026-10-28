import math

import pytest
from pyproj import Geod

from detection.geolocate import Camera, estimate_location
from steeleagle_protocol.v1.common import common_pb2
from steeleagle_protocol.v1.messages.telemetry import telemetry_pb2

WGS84 = Geod(ellps="WGS84")
HFOV = 69.0
VFOV = 43.0
START_LAT = 40.4433
START_LON = -79.9436
HEIGHT_M = 30.0
# Metres of ground error tolerated; the model is exact but floats are not.
TOLERANCE_M = 0.01

CAMERA = Camera(hfov_deg=HFOV, vfov_deg=VFOV)


def position(heading=0.0, height=HEIGHT_M):
    return telemetry_pb2.PositionInfo(
        global_position=common_pb2.GlobalPosition(
            latitude=START_LAT, longitude=START_LON, heading=heading
        ),
        relative_position=common_pb2.RelativePosition(z=height),
    )


def gimbal(pitch):
    return telemetry_pb2.GimbalInfo(pose_body=common_pb2.Pose(pitch=pitch))


def ground_distance_m(lat, lon):
    _, _, distance = WGS84.inv(START_LON, START_LAT, lon, lat)
    return distance


def bearing_deg(lat, lon):
    azimuth, _, _ = WGS84.inv(START_LON, START_LAT, lon, lat)
    return azimuth % 360


def test_box_bottom_on_the_optical_axis_lands_where_the_gimbal_points():
    # Gimbal 45 degrees down, box bottom on the optical axis: the ray hits the
    # ground at height / tan(45 degrees) = height metres ahead.
    box = (0.4, 0.4, 0.5, 0.6)  # y_min, x_min, y_max, x_max
    lat, lon = estimate_location(box, position(heading=0.0), gimbal(-45.0), CAMERA)
    assert ground_distance_m(lat, lon) == pytest.approx(HEIGHT_M, abs=TOLERANCE_M)
    assert bearing_deg(lat, lon) == pytest.approx(0.0, abs=1e-3)


def test_straight_down_gimbal_puts_the_centered_box_under_the_vehicle():
    box = (0.4, 0.45, 0.5, 0.55)
    lat, lon = estimate_location(box, position(), gimbal(-90.0), CAMERA)
    assert ground_distance_m(lat, lon) == pytest.approx(0.0, abs=TOLERANCE_M)


def test_heading_rotates_the_target_bearing():
    box = (0.4, 0.4, 0.5, 0.6)
    lat, lon = estimate_location(box, position(heading=90.0), gimbal(-45.0), CAMERA)
    assert bearing_deg(lat, lon) == pytest.approx(90.0, abs=1e-3)


def test_pixel_offset_maps_to_ground_with_the_pinhole_tangent_not_a_linear_angle():
    # Looking straight down, a box bottom a quarter of the width right of
    # center is height * (0.25 / focal) metres east, with the focal length in
    # widths, focal = 0.5 / tan(HFOV / 2). A linear angle mapping would give
    # height * tan(0.5 * HFOV / 2), about 10% less.
    box = (0.4, 0.70, 0.5, 0.80)  # bottom-center x = 0.75
    focal = 0.5 / math.tan(math.radians(HFOV / 2))
    lat, lon = estimate_location(box, position(heading=0.0), gimbal(-90.0), CAMERA)
    assert ground_distance_m(lat, lon) == pytest.approx(
        HEIGHT_M * 0.25 / focal, abs=TOLERANCE_M
    )
    assert bearing_deg(lat, lon) == pytest.approx(90.0, abs=1e-3)


def test_target_above_the_horizon_has_no_ground_intersection():
    box = (0.0, 0.4, 0.1, 0.6)  # bottom near the top of the image
    assert estimate_location(box, position(), gimbal(0.0), CAMERA) is None


def test_box_bottom_lower_in_the_image_lands_nearer_the_vehicle():
    high = (0.30, 0.4, 0.40, 0.6)
    low = (0.30, 0.4, 0.80, 0.6)
    far = estimate_location(high, position(), gimbal(-45.0), CAMERA)
    near = estimate_location(low, position(), gimbal(-45.0), CAMERA)
    assert ground_distance_m(*near) < ground_distance_m(*far)


def test_without_gimbal_the_vehicle_position_is_used():
    box = (0.4, 0.4, 0.5, 0.6)
    lat, lon = estimate_location(box, position(), None, CAMERA)
    assert (lat, lon) == pytest.approx((START_LAT, START_LON))
