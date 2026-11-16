"""Vehicle-side frame work: the preprocess engine, which shrinks each camera
frame to SwiftMap's model input, and the keyframe filter reachback runs, so
only keyframes far enough apart cross the network."""

import io
import logging
import math
import time
from collections import Counter

import cv2
import numpy as np
import uriel
from PIL import Image

from steeleagle_protocol.v1.messages.frame import frame_pb2
from swiftmap.protocol import (
    ENCODED_FRAME_TYPE, POLL_INTERVAL_S, RAW_FRAME_TYPE, STATS_INTERVAL_S, WEBP_CODEC, FrameError,
    frame_gps, read_frame, same_type, webp_bytes,
)

logger = logging.getLogger("swiftmap.onboard")

RGB_CHANNELS = 3
MIN_ASPECT, MAX_ASPECT = 0.5, 2.0
FRAME_FIELDS = ("timestamp", "gimbal_info", "position_info")

# ---------------------------------------------------------------- preprocess
# VGGT-Omega's preprocessing, done on board: center-crop an aspect ratio
# outside [0.5, 2], then PIL-bicubic resize to the "balanced" shape, whole
# patches about (image_resolution / patch_size)**2 of them. Sent lossless, the
# server's own preprocessing then changes nothing.


def crop_box(width, height):
    """The center crop bringing height/width into [MIN_ASPECT, MAX_ASPECT]."""
    aspect = height / max(width, 1)
    if aspect < MIN_ASPECT:
        keep = min(width, max(1, round(height / MIN_ASPECT)))
        left = max((width - keep) // 2, 0)
        return left, 0, left + keep, height
    if aspect > MAX_ASPECT:
        keep = min(height, max(1, round(width * MAX_ASPECT)))
        top = max((height - keep) // 2, 0)
        return 0, top, width, top + keep
    return 0, 0, width, height


def model_size(width, height, image_resolution, patch_size):
    """(width, height) of the model input for a frame of this size."""
    tokens = (image_resolution // patch_size) ** 2
    across = math.sqrt(tokens / (height / max(width, 1)))
    down = tokens / across
    return max(1, round(across)) * patch_size, max(1, round(down)) * patch_size


def preprocess(image, image_resolution, patch_size):
    """The PIL RGB `image`, cropped and resized to its model input."""
    box = crop_box(*image.size)
    if box != (0, 0, *image.size):
        image = image.crop(box)
    return image.resize(model_size(*image.size, image_resolution, patch_size),
                        Image.Resampling.BICUBIC)


def encode_frame(data, *, image_resolution, patch_size, lossless, quality):
    """The serialized RGB RawFrame `data`, preprocessed, as a serialized WebP
    EncodedFrame keeping the frame's id, timestamp and pose."""
    frame = frame_pb2.RawFrame.FromString(data)
    width, height = frame.h_res, frame.v_res
    if frame.channels != RGB_CHANNELS or width == 0 or len(frame.data) != width * height * RGB_CHANNELS:
        raise FrameError(f"{len(frame.data)} bytes is not a {width}x{height} RGB frame")
    image = preprocess(Image.frombytes("RGB", (width, height), frame.data), image_resolution, patch_size)
    buf = io.BytesIO()
    image.save(buf, format="WEBP", lossless=lossless, quality=quality)
    out = frame_pb2.EncodedFrame(encoded_data=buf.getvalue(), id=frame.id,
                                 camera_id=frame.camera_id, codec=WEBP_CODEC, keyframe=True)
    for field in FRAME_FIELDS:
        if frame.HasField(field):
            getattr(out, field).CopyFrom(getattr(frame, field))
    return out.SerializeToString()


def run_preprocess(name, args, *, stop):
    """Serves the preprocess engine `name` until `stop` is set."""
    options = {k: args[k] for k in ("image_resolution", "patch_size", "lossless", "quality")}
    limit = args["max_output_size"]
    with uriel.Engine(name, max_output_size=limit) as engine:
        logger.info("engine %s ready; model input %d px, patch %d, %s WebP", name,
                    args["image_resolution"], args["patch_size"],
                    "lossless" if args["lossless"] else f"quality {args['quality']}")
        while not stop.is_set():
            engine.wait(POLL_INTERVAL_S)
            if (input := engine.poll()) is None:
                continue
            try:
                if not same_type(input.payload_type, RAW_FRAME_TYPE):
                    raise FrameError(f"not a {RAW_FRAME_TYPE}")
                payload = encode_frame(bytes(input.data), **options)
                if len(payload) > limit:
                    raise FrameError(f"{len(payload)} bytes, over max_output_size")
            except (FrameError, ValueError) as err:
                logger.warning("input %d: %s; dropping it", input.id.seq, err)
                engine.drop()
                continue
            engine.loan()[:len(payload)] = payload
            engine.replace(len(payload), output_type=ENCODED_FRAME_TYPE)


# ------------------------------------------------------------------ keyframes
# The SwiftMap server's optical-flow test: a frame is the next keyframe once
# the last keyframe's features, tracked into it with Lucas-Kanade, moved more
# than min_disparity px on average (mean of |dx| and |dy| together).

DEFAULT_MIN_DISPARITY = 40.0
FEATURES = dict(maxCorners=1000, qualityLevel=0.01, minDistance=8, blockSize=7)
FLOW = dict(winSize=(21, 21), maxLevel=3,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))
MIN_TRACKING_POINTS = 10
EARTH_RADIUS_M = 6_371_000.0
NO_FIX, TOO_CLOSE, NOT_KEYFRAME = "without a GPS fix", "within send_distance", "not keyframes"


class FrameTracker:
    """Picks the keyframes of one camera."""

    def __init__(self, min_disparity=DEFAULT_MIN_DISPARITY):
        self.min_disparity = min_disparity
        self.kf_gray = self.kf_pts = None
        # Disparity of the last frame compared with the keyframe, or None
        self.last_disparity = None

    def _set_keyframe(self, gray):
        """Makes `gray` the keyframe; whether it has enough features to track."""
        self.kf_gray = gray
        self.kf_pts = cv2.goodFeaturesToTrack(gray, **FEATURES)
        return self.kf_pts is not None and len(self.kf_pts) >= MIN_TRACKING_POINTS

    def is_keyframe(self, gray):
        """Whether the grayscale frame `gray` is the next keyframe."""
        self.last_disparity = None
        if self.min_disparity <= 0:
            return True
        if (self.kf_pts is None or len(self.kf_pts) < MIN_TRACKING_POINTS
                or gray.shape != self.kf_gray.shape):
            return self._set_keyframe(gray)
        pts, status, _ = cv2.calcOpticalFlowPyrLK(self.kf_gray, gray, self.kf_pts, None, **FLOW)
        tracked = status.ravel() == 1
        if np.count_nonzero(tracked) < MIN_TRACKING_POINTS:
            return self._set_keyframe(gray)
        self.last_disparity = float(np.mean(np.abs(pts[tracked] - self.kf_pts[tracked])))
        return self.last_disparity > self.min_disparity and self._set_keyframe(gray)


def haversine_m(a, b):
    """Great-circle distance in meters between two (lat, lon, ...) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def to_gray(frame):
    """The WebP EncodedFrame decoded to 8-bit grayscale."""
    gray = cv2.imdecode(np.frombuffer(webp_bytes(frame), np.uint8), cv2.IMREAD_GRAYSCALE)
    if gray is None:
        raise FrameError("failed to decode its WebP")
    return gray


class Stream:
    """One producer's session: its tracker and where its last kept frame was."""

    def __init__(self, session, min_disparity):
        self.session = session
        self.tracker = FrameTracker(min_disparity)
        self.last_gps = None


class KeyframeFilter:
    """Keeps frames at least send_distance m from the last one kept that are
    also keyframes; each producer on its own."""

    def __init__(self, *, min_disparity, send_distance):
        self.min_disparity = min_disparity
        self.send_distance = send_distance
        self._streams = {}
        self.received = self.kept = 0
        self.dropped = Counter()
        self._last = (0, 0, Counter(), time.monotonic())

    def stream(self, id):
        stream = self._streams.get(id.producer)
        if stream is None or stream.session != id.session:
            stream = self._streams[id.producer] = Stream(id.session, self.min_disparity)
        return stream

    def held_back(self, stream, gps):
        """Why a frame at `gps` is held back (no fix, too close), or None."""
        if self.send_distance <= 0:
            return None
        if gps is None:
            return NO_FIX
        if stream.last_gps is not None and haversine_m(stream.last_gps, gps) < self.send_distance:
            return TOO_CLOSE
        return None

    def keep(self, input):
        """Whether to forward `input`; FrameError if it is not a WebP frame."""
        self._log_stats()
        frame = read_frame(input.payload_type, bytes(input.data))
        stream = self.stream(input.id)
        gps = frame_gps(frame)
        # Distance first: cheaper, and the tracker then only sees frames that may be kept
        reason = self.held_back(stream, gps)
        if reason is None and not stream.tracker.is_keyframe(to_gray(frame)):
            reason = NOT_KEYFRAME
        if reason is None:
            stream.last_gps = gps
            self.kept += 1
        else:
            self.dropped[reason] += 1
        logger.debug("input %d of %s: %s (disparity %s)", input.id.seq, input.id.producer,
                     f"dropped, {reason}" if reason else "kept", stream.tracker.last_disparity)
        return reason is None

    def _log_stats(self):
        self.received += 1
        received, kept, dropped, since = self._last
        elapsed = time.monotonic() - since
        if elapsed <= STATS_INTERVAL_S:
            return
        why = ", ".join(f"{n} {r}" for r, n in (self.dropped - dropped).most_common())
        logger.info("%.2f fps; kept %d of the last %d frames%s (%d of %d in all)",
                    (self.received - received) / elapsed, self.kept - kept, self.received - received,
                    f"; dropped {why}" if why else "", self.kept, self.received)
        self._last = (self.received, self.kept, self.dropped.copy(), time.monotonic())
