from panda3d.core import LPoint3f, LVector3f
import math

"""Utility functions for the simulator."""

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
