"""Saves the frames the engine receives as JPEGs, to inspect their quality.

Frames are written on a thread of their own, so saving never holds up
inference; if the disk cannot keep up, frames are skipped rather than queued
without bound.
"""

import logging
import queue
import re
import threading
from datetime import datetime, timezone

from PIL import Image

logger = logging.getLogger("detection.frames")

JPEG_QUALITY = 95
# Frames waiting to be written; past this, new frames are skipped.
QUEUE_SIZE = 8
# How often skipped frames are reported, in frames skipped.
SKIP_REPORT_INTERVAL = 100
# Characters a producer name may keep in a directory name.
UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_STOP = object()


def producer_dir_name(producer):
    """A directory name for `producer`'s frames that cannot leave the
    directory it is in."""
    name = UNSAFE_CHARS.sub("_", producer or "")
    return name if name.strip(".") else "unknown"


def frame_file_name(seq, now=None):
    """`<UTC time>_<seq>.jpg`: sorts by arrival, and stays unique when a
    producer restarts its sequence numbers."""
    now = now or datetime.now(timezone.utc)
    return f"{now:%Y%m%dT%H%M%S.%f}_{seq:08d}.jpg"


class FrameSaver:
    """Writes RawFrames as JPEGs to `directory/<producer>/`."""

    def __init__(self, directory, quality=JPEG_QUALITY, queue_size=QUEUE_SIZE):
        self.directory = directory
        self.quality = quality
        self.skipped = 0
        self._queue = queue.Queue(maxsize=queue_size)
        self._thread = threading.Thread(
            target=self._write_all, name="frame-saver", daemon=True
        )
        self._thread.start()
        logger.info("saving received frames to %s", directory)

    def submit(self, producer, seq, frame):
        """Queues the RawFrame `frame` to be saved; skips it if the queue is
        full."""
        try:
            self._queue.put_nowait(
                (producer, seq, frame.h_res, frame.v_res, frame.channels, frame.data)
            )
        except queue.Full:
            self.skipped += 1
            if self.skipped % SKIP_REPORT_INTERVAL == 1:
                logger.warning(
                    "frame saving is behind; %d frames skipped so far", self.skipped
                )

    def close(self):
        """Writes the frames already queued, then stops."""
        self._queue.put(_STOP)
        self._thread.join()

    def _write_all(self):
        while (item := self._queue.get()) is not _STOP:
            producer, seq, *frame = item
            try:
                self._write(producer, seq, *frame)
            except Exception:
                logger.exception("could not save frame %d of %s", seq, producer)

    def _write(self, producer, seq, width, height, channels, data):
        if channels != 3:
            raise ValueError(f"frame has {channels} channels; only RGB is saved")
        image = Image.frombytes("RGB", (width, height), data)
        directory = self.directory / producer_dir_name(producer)
        directory.mkdir(parents=True, exist_ok=True)
        image.save(directory / frame_file_name(seq), "JPEG", quality=self.quality)
