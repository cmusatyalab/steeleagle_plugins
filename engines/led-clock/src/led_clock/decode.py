"""Reads the LED clock off a camera frame.

The clock (ledclock.py on the Pi) shows `tick = floor(CLOCK_REALTIME / tick)
mod 2**BITS` as Gray code on a row of red LEDs, between two yellow markers that
are always on. The LEDs sit on breadboard rows; the camera faces the board, so
the marker on the left of the image is the one at row LEFT_MARKER_ROW, and bit
0 is the leftmost data LED.

Each frame, the markers are found again (stabilization moves the image), the
LEDs are sampled at their rows' places between them, and each LED is read as
on or off against a threshold of its own, from the levels it has shown lately.
"""

from collections import deque
from dataclasses import dataclass

import cv2
import numpy as np

BITS = 10
# Breadboard rows, as numbered on the board, of the markers and of bits 0 to 9
LEFT_MARKER_ROW = 59
RIGHT_MARKER_ROW = 3
BIT_ROWS = np.array([54, 49, 44, 39, 34, 29, 24, 19, 14, 9])
# Where each LED lies on the line from the left marker to the right one
BIT_FRACTIONS = (LEFT_MARKER_ROW - BIT_ROWS) / (LEFT_MARKER_ROW - RIGHT_MARKER_ROW)
# Spacing of adjacent data LEDs, as a fraction of the distance between markers
BIT_SPACING_FRACTION = (BIT_ROWS[0] - BIT_ROWS[1]) / (LEFT_MARKER_ROW - RIGHT_MARKER_ROW)
# Radius of the square patch an LED is sampled over, as a fraction of the LED
# spacing; it stays inside the LED's glow without reaching its neighbors
PATCH_FRACTION = 0.2
MIN_PATCH_RADIUS_PX = 1

# Yellow, in OpenCV's HSV (hue 0-180). The hue range stops short of orange,
# which a lit red LED can look like where it saturates the sensor.
YELLOW_LOW = (22, 100, 120)
YELLOW_HIGH = (38, 255, 255)
# Smallest yellow blob taken for a marker, in pixels
MIN_MARKER_AREA_PX = 4
MARKER_COUNT = 2

# Frames whose LED levels set the thresholds: 10 s at 30 fps, longer than the
# 2**BITS ticks (5.12 s at 5 ms) in which every bit is both on and off
HISTORY_FRAMES = 300
# Percentiles of an LED's recent levels taken as its off and on levels; not
# the extremes, so that one odd frame does not move the threshold
OFF_PERCENTILE = 5
ON_PERCENTILE = 95
# Least difference between an LED's on and off levels for it to be read
MIN_CONTRAST = 20.0


@dataclass
class Reading:
    """What was read off a frame. `markers` is None if they were not found;
    `code` is None if the LEDs could not be read."""

    markers: tuple[np.ndarray, np.ndarray] | None = None
    positions: np.ndarray | None = None
    patch_radius: int = 0
    levels: np.ndarray | None = None
    thresholds: np.ndarray | None = None
    bits: np.ndarray | None = None
    gray: int | None = None
    code: int | None = None
    # How clearly the least clear LED was on or off: 0 at its threshold, 1 at
    # its usual on or off level
    confidence: float = 0.0


def gray_to_binary(gray):
    binary = gray
    while gray:
        gray >>= 1
        binary ^= gray
    return binary


def find_markers(rgb):
    """The centers (x, y) of the two yellow markers in the RGB image `rgb`,
    left one first, or None unless two are found."""
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    mask = cv2.inRange(hsv, YELLOW_LOW, YELLOW_HIGH)
    count, _, stats, centroids = cv2.connectedComponentsWithStats(mask)
    # Label 0 is the background
    areas = stats[1:, cv2.CC_STAT_AREA]
    blobs = np.flatnonzero(areas >= MIN_MARKER_AREA_PX) + 1
    if len(blobs) < MARKER_COUNT:
        return None
    largest = blobs[np.argsort(stats[blobs, cv2.CC_STAT_AREA])[-MARKER_COUNT:]]
    left, right = sorted((centroids[label] for label in largest), key=lambda c: c[0])
    return left, right


def led_positions(left, right):
    """The centers (x, y) of the data LEDs, bit 0 first, given the markers'."""
    left, right = np.asarray(left, dtype=float), np.asarray(right, dtype=float)
    return left + BIT_FRACTIONS[:, None] * (right - left)


def sample_leds(rgb, positions, radius):
    """The brightness of each LED: the mean, over a square patch around it, of
    each pixel's brightest channel."""
    value = rgb.max(axis=2)
    height, width = value.shape
    levels = np.empty(len(positions))
    for i, (x, y) in enumerate(np.rint(positions).astype(int)):
        patch = value[
            max(y - radius, 0) : min(y + radius + 1, height),
            max(x - radius, 0) : min(x + radius + 1, width),
        ]
        levels[i] = patch.mean() if patch.size else np.nan
    return levels


def display_time_ns(code, recv_ns, tick_ns, bits=BITS):
    """When the clock showed the tick `code` (mod 2**bits) last before
    `recv_ns`: the middle of that tick, so rounding is unbiased."""
    recv_tick = recv_ns // tick_ns
    tick = recv_tick - (recv_tick - code) % (1 << bits)
    return tick * tick_ns + tick_ns // 2


class LedReader:
    """Reads the clock off frames, learning each LED's on and off levels from
    the frames it reads."""

    def __init__(self, history=HISTORY_FRAMES):
        self._history = deque(maxlen=history)

    def read(self, rgb):
        reading = Reading()
        reading.markers = find_markers(rgb)
        if reading.markers is None:
            return reading
        left, right = reading.markers
        reading.positions = led_positions(left, right)
        spacing = np.linalg.norm(right - left) * BIT_SPACING_FRACTION
        reading.patch_radius = max(MIN_PATCH_RADIUS_PX, round(spacing * PATCH_FRACTION))
        reading.levels = sample_leds(rgb, reading.positions, reading.patch_radius)
        if np.isnan(reading.levels).any():
            return reading
        self._history.append(reading.levels)

        off, on = np.percentile(self._history, [OFF_PERCENTILE, ON_PERCENTILE], axis=0)
        if np.any(on - off < MIN_CONTRAST):
            return reading
        reading.thresholds = (on + off) / 2
        reading.bits = reading.levels > reading.thresholds
        margins = np.abs(reading.levels - reading.thresholds) / ((on - off) / 2)
        reading.confidence = float(np.clip(margins.min(), 0.0, 1.0))
        reading.gray = int(np.dot(reading.bits, 1 << np.arange(BITS)))
        reading.code = gray_to_binary(reading.gray)
        return reading
