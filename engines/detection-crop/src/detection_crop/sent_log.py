"""The sent log: one CSV row per crop the engine cuts and queues to send,
with where in its frame it was cut, so that what reaches the verifier can be
scored against labeled boxes. A crop keeps the capture timestamp of its
frame, which the encoders and decoders on the way carry through while they
renumber ids, so a row is matched to a received crop by its timestamp_ns.
"""

import csv
import os
from pathlib import Path

HEADER = (
    "frame_id",
    "timestamp_ns",
    "left",
    "top",
    "right",
    "bottom",
    "frame_width",
    "frame_height",
    "score",
    "class",
)


def log_path(path):
    """`path`, or, if it is relative, that path in the daemon's files
    directory (STEELEAGLE_FILES), with its directory created."""
    path = Path(path)
    files = os.environ.get("STEELEAGLE_FILES")
    if not path.is_absolute() and files:
        path = Path(files) / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class SentLog:
    """Writes the rows to the CSV file `path`, replacing it. Line-buffered,
    so the rows are on disk even if the plugin is killed."""

    def __init__(self, path):
        self.path = log_path(path)
        self._file = open(self.path, "w", newline="", buffering=1)
        self._writer = csv.writer(self._file)
        self._writer.writerow(HEADER)

    def write(self, frame, box, width, height, score, class_name):
        """Logs the crop of the RawFrame `frame` (its id and timestamp) to
        the pixel `box` (left, top, right, bottom) of its `width` x `height`
        frame."""
        left, top, right, bottom = box
        self._writer.writerow(
            (
                frame.id,
                frame.timestamp.ToNanoseconds(),
                left,
                top,
                right,
                bottom,
                width,
                height,
                f"{score:.4f}",
                class_name,
            )
        )

    def close(self):
        self._file.close()
