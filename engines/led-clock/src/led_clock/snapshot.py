"""Annotated snapshots: a frame with what was read off the LED clock drawn on
it, so a person can check that the lit LEDs match the bits read."""

import logging

import cv2
import numpy as np

logger = logging.getLogger("led_clock.snapshot")

JPEG_QUALITY = 90
MARKER_COLOR = (255, 220, 0)
ON_COLOR = (0, 255, 0)
OFF_COLOR = (128, 128, 128)
UNREAD_COLOR = (255, 0, 255)
TEXT_COLOR = (255, 255, 255)
TEXT_SHADOW = (0, 0, 0)
LINE_PX = 2
FONT = cv2.FONT_HERSHEY_SIMPLEX
FONT_SCALE = 0.7
TEXT_ORIGIN = (10, 30)
LINE_SPACING_PX = 28
# Rings around the markers and LEDs, as a multiple of the sampled patch
RING_SCALE = 2
# The bits are labelled this far below each LED, in patch radii
LABEL_OFFSET_SCALE = 5


def annotate(rgb, reading, lines):
    """A copy of the RGB image `rgb` with the LedReader `reading` drawn on it,
    and the text `lines` at its top left."""
    image = np.ascontiguousarray(rgb).copy()
    ring = max(reading.patch_radius * RING_SCALE, LINE_PX)
    if reading.markers is not None:
        for x, y in np.rint(reading.markers).astype(int):
            cv2.circle(image, (x, y), ring, MARKER_COLOR, LINE_PX)
    if reading.positions is not None:
        for i, (x, y) in enumerate(np.rint(reading.positions).astype(int)):
            if reading.bits is None:
                color, label = UNREAD_COLOR, "?"
            else:
                on = bool(reading.bits[i])
                color, label = (ON_COLOR, "1") if on else (OFF_COLOR, "0")
            cv2.circle(image, (x, y), ring, color, LINE_PX)
            label_at = (x - ring // 2, y + reading.patch_radius * LABEL_OFFSET_SCALE + ring)
            _text(image, label, label_at)
    for i, line in enumerate(lines):
        _text(image, line, (TEXT_ORIGIN[0], TEXT_ORIGIN[1] + i * LINE_SPACING_PX))
    return image


def _text(image, text, origin):
    cv2.putText(image, text, origin, FONT, FONT_SCALE, TEXT_SHADOW, LINE_PX * 2, cv2.LINE_AA)
    cv2.putText(image, text, origin, FONT, FONT_SCALE, TEXT_COLOR, LINE_PX, cv2.LINE_AA)


class JpegSaver:
    """Saves RGB images as JPEGs in `directory`."""

    def __init__(self, directory, quality=JPEG_QUALITY):
        self.directory = directory
        self.quality = quality
        directory.mkdir(parents=True, exist_ok=True)
        logger.info("saving snapshots to %s", directory)

    def save(self, name, rgb):
        path = self.directory / name
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        if not cv2.imwrite(str(path), bgr, [cv2.IMWRITE_JPEG_QUALITY, self.quality]):
            logger.warning("could not save %s", path)
