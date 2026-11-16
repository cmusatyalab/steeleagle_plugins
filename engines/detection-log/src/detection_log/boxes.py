"""Writes the detector's boxes on each frame to a CSV file, a row per box, for
scoring a detector against labels and measuring its latency.

Columns:

- frame_id: the RawFrame's id (a driver's frame number, e.g. fake_drone's,
  kept across H.264 by the encoder's pass_through_id);
- timestamp_ns: the RawFrame's timestamp, when the driver captured it;
- result_ns: when the detector's result reached this engine, by this host's
  wall clock, so result_ns - timestamp_ns is the frame's latency from capture
  (across hosts, give or take their clocks' offset);
- seq: the sequence number of the frame's input;
- class, score, x_min, y_min, x_max, y_max: a detection, its box normalized
  to the frame; all empty on the one row of a frame with no detections;
- frame_width, frame_height: the frame's size in pixels.
"""

import csv
import logging

logger = logging.getLogger("detection_log.boxes")

COLUMNS = (
    "frame_id",
    "timestamp_ns",
    "result_ns",
    "seq",
    "class",
    "score",
    "x_min",
    "y_min",
    "x_max",
    "y_max",
    "frame_width",
    "frame_height",
)
# Rows written between flushes, so a trial stopped at any time loses few
FLUSH_EVERY = 32


def timestamp_ns(frame):
    """The RawFrame `frame`'s timestamp in nanoseconds, or empty if unset."""
    if not frame.HasField("timestamp"):
        return ""
    return frame.timestamp.seconds * 1_000_000_000 + frame.timestamp.nanos


class BoxLog:
    """Appends the boxes of each frame to the CSV file `path`, replacing it."""

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = open(path, "w", newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow(COLUMNS)
        self._unflushed = 0
        logger.info("logging boxes to %s", path)

    def write(self, seq, frame, result, result_ns):
        """Writes a row for each detection of the DetectionResult `result` on
        the RawFrame `frame`, of the input `seq`, which reached the engine at
        `result_ns`; a row with no detection if it has none."""
        head = (frame.id, timestamp_ns(frame), result_ns, seq)
        size = (frame.h_res, frame.v_res)
        rows = [
            (
                *head,
                d.class_name,
                f"{d.score:.4f}",
                *(f"{v:.5f}" for v in (d.bbox.x_min, d.bbox.y_min, d.bbox.x_max, d.bbox.y_max)),
                *size,
            )
            for d in result.detections
        ] or [(*head, "", "", "", "", "", "", *size)]
        self._writer.writerows(rows)
        self._unflushed += 1
        if self._unflushed >= FLUSH_EVERY:
            self._file.flush()
            self._unflushed = 0

    def close(self):
        self._file.close()
