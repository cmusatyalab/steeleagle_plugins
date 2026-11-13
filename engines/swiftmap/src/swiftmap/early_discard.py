"""Early discard for SwiftMap, which the reachback engine runs on the
vehicle. A frame is kept if it was taken at least send_distance meters from
the last frame kept, and is a keyframe by the optical-flow test the SwiftMap
server applies (swiftmap.keyframe). Every other frame is dropped where it was
taken, so it never crosses the network. Each producer (camera) is tracked on
its own, and starts over with each new session."""

import logging
import math
import time
from collections import Counter

import cv2
import numpy as np

from steeleagle_protocol.v1.messages.frame import frame_pb2
from swiftmap.engine import FrameError, frame_gps, jpeg_bytes, read_frame, rgb_pixels
from swiftmap.keyframe import FrameTracker

logger = logging.getLogger("swiftmap.keyframe")

# Seconds between stats log lines.
STATS_INTERVAL_S = 5.0

EARTH_RADIUS_M = 6_371_000.0

# Why a frame was dropped, for the stats.
NO_FIX = "without a GPS fix"
TOO_CLOSE = "within send_distance"
NOT_KEYFRAME = "not keyframes"


def haversine_m(a, b):
    """Great-circle distance in meters between two (lat, lon, ...) points."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def to_gray(frame):
    """The frame's image in 8-bit grayscale: a RawFrame's RGB pixels, or a
    JPEG EncodedFrame decoded."""
    if isinstance(frame, frame_pb2.EncodedFrame):
        gray = cv2.imdecode(np.frombuffer(jpeg_bytes(frame), np.uint8), cv2.IMREAD_GRAYSCALE)
        if gray is None:
            raise FrameError("failed to decode its JPEG")
        return gray
    return cv2.cvtColor(rgb_pixels(frame), cv2.COLOR_RGB2GRAY)


class Stream:
    """What the filter keeps of one producer's session: its keyframe tracker,
    and where the last frame it forwarded was taken."""

    def __init__(self, session, min_disparity):
        self.session = session
        self.tracker = FrameTracker(min_disparity)
        self.last_gps = None


class KeyframeFilter:
    """Decides which frames to forward: those taken send_distance meters or
    more from the last one forwarded that are also keyframes. Each producer
    is tracked on its own."""

    def __init__(self, *, min_disparity, send_distance):
        self.min_disparity = min_disparity
        self.send_distance = send_distance
        # producer -> its Stream
        self._streams = {}
        self.received = 0
        self.kept = 0
        self.dropped = Counter()
        self._last_received = 0
        self._last_kept = 0
        self._last_dropped = Counter()
        self._last_stats = time.monotonic()

    def stream(self, id):
        """The Stream of the input `id`'s producer, new if its session is."""
        stream = self._streams.get(id.producer)
        if stream is None or stream.session != id.session:
            stream = self._streams[id.producer] = Stream(id.session, self.min_disparity)
        return stream

    def held_back(self, stream, gps):
        """Why a frame taken at `gps` is held back, or None if it is not: it
        has no fix, or is within send_distance of the last frame forwarded.
        Without send_distance, no frame is."""
        if self.send_distance <= 0:
            return None
        if gps is None:
            return NO_FIX
        if stream.last_gps is not None and haversine_m(stream.last_gps, gps) < self.send_distance:
            return TOO_CLOSE
        return None

    def keep(self, input):
        """Whether to forward `input`. Raises FrameError if it is not a frame
        this engine reads."""
        self._log_stats()
        frame = read_frame(input.payload_type, bytes(input.data))
        stream = self.stream(input.id)
        gps = frame_gps(frame)
        # The distance first: it is cheaper, and the tracker then only sees
        # frames that may be forwarded, so its keyframe is the last frame sent
        reason = self.held_back(stream, gps)
        if reason is None and not stream.tracker.is_keyframe(to_gray(frame)):
            reason = NOT_KEYFRAME
        if reason is not None:
            self.dropped[reason] += 1
        else:
            stream.last_gps = gps
            self.kept += 1
        if logger.isEnabledFor(logging.DEBUG):
            disparity = stream.tracker.last_disparity
            logger.debug("input %d of %s: %s (disparity %s)", input.id.seq, input.id.producer,
                         f"dropped, {reason}" if reason else "forwarded",
                         "none" if disparity is None else f"{disparity:.1f}")
        return reason is None

    def _log_stats(self):
        self.received += 1
        now = time.monotonic()
        elapsed = now - self._last_stats
        if elapsed > STATS_INTERVAL_S:
            received = self.received - self._last_received
            kept = self.kept - self._last_kept
            dropped = self.dropped - self._last_dropped
            why = ", ".join(f"{n} {reason}" for reason, n in dropped.most_common())
            logger.info("%.2f fps; kept %d of the last %d frames%s (%d of %d in all)",
                        received / elapsed, kept, received, f"; dropped {why}" if why else "",
                        self.kept, self.received)
            self._last_received, self._last_kept = self.received, self.kept
            self._last_dropped = self.dropped.copy()
            self._last_stats = now

