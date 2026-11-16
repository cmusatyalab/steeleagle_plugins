"""Reads a directory of still images as if it were a drone's live camera,
for datasets that come as images rather than a video (e.g. SARD, WiSARD).

The images are played in the order of their file names, sorted naturally
(frame_2 before frame_10), one every 1/fps seconds, so frame i of the
playback is the i-th image of that order, and gets frame id i. An image
that can't be decoded is sent as a black frame, so the ids stay aligned with
the order. Images wider than `max_width` are scaled down, keeping their
aspect ratio, so labels given relative to the image size still apply.
ImageCapture reads like GstCapture: isOpened, read, release and ended.
"""
import logging
import re
import time
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger('fake-drone/images')

# File extensions played, ignoring case; anything else (e.g. label files
# next to the images) is skipped.
IMAGE_EXTENSIONS = frozenset({'.jpg', '.jpeg', '.png', '.webp', '.bmp'})


def natural_key(name):
    """Sort key that orders the digit runs of `name` by their value."""
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r'(\d+)', name)]


def list_images(directory):
    """The image files directly in `directory`, in playback order."""
    files = [p for p in Path(directory).iterdir()
             if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
    return sorted(files, key=lambda p: natural_key(p.name))


class ImageCapture:
    """Plays the images in `directory` at `fps`, looping unless `loop` is
    false, in which case `ended` is set once the last one is read. Each read
    returns (True, an RGB frame, its time in seconds into the playback), or
    (False, None, None) after the end. `max_width` (0 for none) caps the
    width of the frames."""

    def __init__(self, directory, fps, loop=True, max_width=0):
        self._max_width = max_width
        self._shape = None
        self._files = list_images(directory)
        self._period = 1.0 / fps
        self._loop = loop
        self._index = 0
        self._deadline = None
        self.ended = False
        self._opened = bool(self._files)
        if self._opened:
            logger.info(f'playing {len(self._files)} images from {directory} at {fps} fps')
        else:
            logger.warning(f'no images ({", ".join(sorted(IMAGE_EXTENSIONS))}) in {directory}')

    def isOpened(self):
        return self._opened

    def frame_index(self, pts):
        """The index of the image read at `pts` (index * period)."""
        return None if pts is None else round(pts / self._period)

    def read(self):
        if not self._opened:
            return False, None, None
        if self._index >= len(self._files):
            if not self._loop:
                logger.info('the images ended')
                self.ended = True
                self._opened = False
                return False, None, None
            logger.info('looping the images back to the first')
            self._index = 0
        # Pace like a camera: one frame per period, skipping ahead rather
        # than bursting if a slow read made it fall behind
        now = time.monotonic()
        if self._deadline is None:
            self._deadline = now
        elif self._deadline > now:
            time.sleep(self._deadline - now)
        else:
            self._deadline = now
        self._deadline += self._period

        index = self._index
        self._index += 1
        path = self._files[index]
        bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if bgr is None:
            logger.warning(f'could not decode {path}; sending a black frame in its place')
            rgb = np.zeros(self._shape or (720, 1280, 3), dtype=np.uint8)
            return True, rgb, index * self._period
        height, width = bgr.shape[:2]
        if self._max_width and width > self._max_width:
            scale = self._max_width / width
            size = (self._max_width, max(2, round(height * scale / 2) * 2))
            bgr = cv2.resize(bgr, size, interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        self._shape = rgb.shape
        return True, rgb, index * self._period

    def release(self):
        self._opened = False
