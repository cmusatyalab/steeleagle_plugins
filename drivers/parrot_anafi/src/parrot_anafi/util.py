"""Shared utilities for the Parrot Anafi driver."""

from math import sin, cos

import numpy as np

def rotation_matrix(yaw):
    """Rotation matrix taking a body-frame vector into NEU."""
    return np.array([[cos(yaw), -sin(yaw)], [sin(yaw), cos(yaw)]])

def rotate_body_to_neu(x, y, yaw):
    """Rotates a body-frame vector into NEU."""
    north, east = rotation_matrix(yaw) @ np.array([x, y])
    return north, east

def rotate_neu_to_body(x, y, yaw):
    """Rotates a NEU vector into body-frame."""
    forward, right = rotation_matrix(yaw).T @ np.array([x, y])
    return forward, right
