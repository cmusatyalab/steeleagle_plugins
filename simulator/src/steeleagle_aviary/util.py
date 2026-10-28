from panda3d.core import LPoint3f, LVector3f
from math import sin, cos
import math
import numpy as np

"""Utility functions for the simulator."""

def rotation_matrix(theta: float) -> np.ndarray:
    """Rotation matrix taking a body-frame (forward, right) vector into NEU.

    `theta` is the vehicle's raw simulator heading (`Vehicle.current_rotation().x`),
    in radians, matching the forward/right basis used elsewhere in `Vehicle`.
    """
    return np.array([[cos(theta), sin(theta)], [-sin(theta), cos(theta)]])

def rotate_body_to_neu(forward: float, right: float, theta: float) -> tuple[float, float]:
    """Rotate a body-frame (forward, right) vector into NEU (north, east)."""
    north, east = rotation_matrix(theta) @ np.array([forward, right])
    return north, east

def rotate_neu_to_body(north: float, east: float, theta: float) -> tuple[float, float]:
    """Rotate a NEU (north, east) vector into body-frame (forward, right)."""
    forward, right = rotation_matrix(theta).T @ np.array([north, east])
    return forward, right

def convert_angle_heading(inp: float):
    """Convert angle to heading.

    Converts a simulator angle to a heading and vice-versa. This
    function is self-invertible.
    """
    d = -inp
    return (d + 360) % 360

def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the bearing from one geodetic point to another.

    Gets the bearing between two geodetic points. This is useful
    for functions that require the vehicle to face a target
    geodetic point.
    """
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])

    delta_lon = lon2 - lon1

    # Bearing calculation
    x = math.sin(delta_lon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)

    initial_bearing = math.atan2(x, y)

    # Convert bearing from radians to degrees
    initial_bearing = math.degrees(initial_bearing)

    # Normalize to 0-360 degrees
    converted_bearing = (initial_bearing + 360) % 360

    return converted_bearing
