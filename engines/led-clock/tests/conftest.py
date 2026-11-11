"""Synthetic camera frames of the LED clock's breadboard."""

import cv2
import numpy as np
import uriel
from steeleagle_protocol.v1.messages.frame import frame_pb2

PRODUCER = "camera"
SESSION = 0xDEADBEEF_CAFEF00D

WIDTH, HEIGHT = 1280, 720
RGB_CHANNELS = 3
BACKGROUND = (40, 40, 40)
YELLOW = (255, 220, 0)
RED_ON = (255, 30, 30)
RED_OFF = (70, 20, 20)
LED_RADIUS_PX = 5
# Pixels between adjacent breadboard rows, and where the leftmost marker is
ROW_PX = 10
LEFT_MARKER = (300, 360)

# Breadboard rows of the LEDs, as numbered on the board (ledclock wiring):
# the left marker, bit 0 to bit 9, and the right marker, as seen facing it.
LEFT_MARKER_ROW = 59
BIT_ROWS = [54 - 5 * bit for bit in range(10)]
RIGHT_MARKER_ROW = 3


def to_gray(tick):
    return tick ^ (tick >> 1)


def row_x(row, left_x=LEFT_MARKER[0]):
    return left_x + (LEFT_MARKER_ROW - row) * ROW_PX


def make_image(gray, *, offset=(0, 0), noise=0, seed=0):
    """An RGB image of the breadboard showing the Gray code `gray`, with the
    LED row moved by `offset` pixels and Gaussian `noise` added."""
    image = np.empty((HEIGHT, WIDTH, RGB_CHANNELS), dtype=np.uint8)
    image[:] = BACKGROUND
    left_x, y = LEFT_MARKER[0] + offset[0], LEFT_MARKER[1] + offset[1]
    for row in (LEFT_MARKER_ROW, RIGHT_MARKER_ROW):
        cv2.circle(image, (row_x(row, left_x), y), LED_RADIUS_PX, YELLOW, -1)
    for bit, row in enumerate(BIT_ROWS):
        color = RED_ON if gray >> bit & 1 else RED_OFF
        cv2.circle(image, (row_x(row, left_x), y), LED_RADIUS_PX, color, -1)
    if noise:
        rng = np.random.default_rng(seed)
        noisy = image.astype(np.int16) + rng.normal(0, noise, image.shape).astype(np.int16)
        image = np.clip(noisy, 0, 255).astype(np.uint8)
    return image


def make_frame(image, *, timestamp_ns=0, frame_id=0):
    frame = frame_pb2.RawFrame(
        data=image.tobytes(),
        h_res=image.shape[1],
        v_res=image.shape[0],
        channels=RGB_CHANNELS,
        id=frame_id,
    )
    frame.timestamp.FromNanoseconds(timestamp_ns)
    return frame


def input_id(seq):
    return uriel.InputId(PRODUCER, SESSION, seq)


class Clock:
    def __init__(self):
        self.ns = 1_000_000_000

    def __call__(self):
        return self.ns

    def advance_s(self, seconds):
        self.ns += int(seconds * 1e9)


class FakeSaver:
    def __init__(self):
        self.saved = []

    def save(self, name, image):
        self.saved.append((name, image))
