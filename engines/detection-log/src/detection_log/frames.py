"""Writes each frame as two JPEGs: the raw frame, and the frame with its
detections drawn on it.

Frames are written on a thread of their own, so the engine never waits on the
disk; if the disk cannot keep up, frames are skipped rather than queued
without bound.
"""

import logging
import queue
import threading

from PIL import Image, ImageDraw

logger = logging.getLogger("detection_log.frames")

JPEG_QUALITY = 75
RGB_CHANNELS = 3
# Frames waiting to be written; past this, new frames are skipped.
QUEUE_SIZE = 8
# How often skipped frames are reported, in frames skipped.
SKIP_REPORT_INTERVAL = 100
BOX_COLOR = (255, 0, 0)
LABEL_COLOR = (255, 255, 255)
# Line width of a box, as a fraction of the frame's longer side, and at least
# MIN_BOX_WIDTH pixels, so JPEG's halved color resolution does not wash it out
BOX_WIDTH_FRACTION = 1 / 200
MIN_BOX_WIDTH = 2
# Subdirectories of the raw frames and of the frames with detections drawn
RAW_DIR = "raw"
OUT_DIR = "out"
_STOP = object()


def frame_file_name(seq):
    """`<seq>.jpeg`, for the sequence number of the frame's input."""
    return f"{seq}.jpeg"


def draw_detections(image, result):
    """Draws each detection of the DetectionResult `result`, whose boxes are
    normalized, on the PIL `image`, labelled with its class and score."""
    width, height = image.size
    line = max(MIN_BOX_WIDTH, round(max(width, height) * BOX_WIDTH_FRACTION))
    draw = ImageDraw.Draw(image)
    for d in result.detections:
        left, top = d.bbox.x_min * width, d.bbox.y_min * height
        right, bottom = d.bbox.x_max * width, d.bbox.y_max * height
        draw.rectangle((left, top, right, bottom), outline=BOX_COLOR, width=line)
        label = f"{d.class_name} {d.score:.2f}"
        text_left, text_top, text_right, text_bottom = draw.textbbox((0, 0), label)
        label_width = text_right - text_left + 2 * line
        label_height = text_bottom - text_top + 2 * line
        # Above the box, or inside its top if the box is at the top of the frame
        label_top = top - label_height if top >= label_height else top
        draw.rectangle(
            (left, label_top, left + label_width, label_top + label_height),
            fill=BOX_COLOR,
        )
        draw.text(
            (left + line - text_left, label_top + line - text_top),
            label,
            fill=LABEL_COLOR,
        )
    return image


class FrameSaver:
    """Writes RawFrames as JPEGs, as is to `directory/raw/<seq>.jpeg` and with
    their detections drawn to `directory/out/<seq>.jpeg`."""

    def __init__(self, directory, quality=JPEG_QUALITY, queue_size=QUEUE_SIZE):
        self.directory = directory
        self.quality = quality
        self.skipped = 0
        self._queue = queue.Queue(maxsize=queue_size)
        self._thread = threading.Thread(
            target=self._write_all, name="frame-saver", daemon=True
        )
        self._thread.start()
        logger.info("saving frames to %s", directory)

    def submit(self, seq, frame, result):
        """Queues the RawFrame `frame` to be saved with the DetectionResult
        `result` drawn on it; skips it if the queue is full. Returns whether
        it was queued."""
        try:
            self._queue.put_nowait((seq, frame, result))
        except queue.Full:
            self.skipped += 1
            if self.skipped % SKIP_REPORT_INTERVAL == 1:
                logger.warning(
                    "frame saving is behind; %d frames skipped so far", self.skipped
                )
            return False
        return True

    def close(self):
        """Writes the frames already queued, then stops."""
        self._queue.put(_STOP)
        self._thread.join()

    def _write_all(self):
        while (item := self._queue.get()) is not _STOP:
            try:
                self._write(*item)
            except Exception:
                logger.exception("could not save frame %d", item[0])

    def _write(self, seq, frame, result):
        width, height = frame.h_res, frame.v_res
        if frame.channels != RGB_CHANNELS:
            raise ValueError(f"frame has {frame.channels} channels; only RGB is saved")
        if len(frame.data) != width * height * RGB_CHANNELS:
            raise ValueError(
                f"{len(frame.data)} bytes is not a {width}x{height} RGB frame"
            )
        image = Image.frombytes("RGB", (width, height), frame.data)
        name = frame_file_name(seq)
        # The raw frame first, as drawing changes the image
        self._save(image, RAW_DIR, name)
        self._save(draw_detections(image, result), OUT_DIR, name)

    def _save(self, image, subdirectory, name):
        directory = self.directory / subdirectory
        directory.mkdir(parents=True, exist_ok=True)
        image.save(directory / name, "JPEG", quality=self.quality)
