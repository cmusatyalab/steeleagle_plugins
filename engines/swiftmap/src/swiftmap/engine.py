"""The swiftmap engine: each frame is forwarded unchanged, and, at most once
per send_distance meters of travel, sent with its GPS fix to the SwiftMap
mapping server. When the server plans a next-flight area, its KML is the
frame's result. The engine's result type is GenericResult, whose payload the
vehicle sets to the result as it is, so the result is the bare KML."""

import logging
import math
import time

import cv2
import numpy as np
import uriel
from google.protobuf.message import DecodeError

from steeleagle_protocol.v1.messages.frame import frame_pb2
from swiftmap.client import ERROR

logger = logging.getLogger("swiftmap")

RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"
ENCODED_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/EncodedFrame"
GENERIC_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/GenericResult"
RGB_CHANNELS = 3
# How long the engine waits for an input before it checks whether to stop.
POLL_INTERVAL_S = 0.1
# Seconds between fps log lines.
STATS_INTERVAL_S = 5.0

EARTH_RADIUS_M = 6_371_000.0


class FrameError(Exception):
    pass


def haversine_m(a, b):
    """Great-circle distance in meters between two (lat, lon, ...) points."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def frame_gps(frame):
    """(lat, lon, alt) of a frame's global position, or None without a fix.
    An unset altitude is 0."""
    if not frame.HasField("position_info") or not frame.position_info.HasField("global_position"):
        return None
    g = frame.position_info.global_position
    if not (g.HasField("latitude") and g.HasField("longitude")):
        return None
    return (g.latitude, g.longitude, g.altitude if g.HasField("altitude") else 0.0)


def read_frame(payload_type, data):
    """The RawFrame or EncodedFrame `data`."""
    try:
        if payload_type == RAW_FRAME_TYPE:
            return frame_pb2.RawFrame.FromString(data)
        if payload_type == ENCODED_FRAME_TYPE:
            return frame_pb2.EncodedFrame.FromString(data)
    except DecodeError as err:
        raise FrameError(f"bad {payload_type}: {err}") from None
    raise FrameError(f"payload is a {payload_type!r}, not a RawFrame or EncodedFrame")


def to_jpeg(frame, quality):
    """The frame's image as a JPEG: an EncodedFrame's as it is, a RawFrame's
    RGB pixels encoded at `quality`."""
    if isinstance(frame, frame_pb2.EncodedFrame):
        if frame.codec != frame_pb2.CODEC_JPEG:
            raise FrameError(f"{frame_pb2.Codec.Name(frame.codec)} frames are not supported, only JPEG")
        return frame.encoded_data
    width, height = frame.h_res, frame.v_res
    if frame.channels != RGB_CHANNELS or len(frame.data) != width * height * RGB_CHANNELS:
        raise FrameError(
            f"{len(frame.data)} bytes is not a {width}x{height} frame of {frame.channels} channels"
        )
    rgb = np.frombuffer(frame.data, dtype=np.uint8).reshape(height, width, RGB_CHANNELS)
    ok, jpeg = cv2.imencode(
        ".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, quality]
    )
    if not ok:
        raise FrameError("failed to encode it as a JPEG")
    return jpeg.tobytes()


class SwiftMap:
    """Decides which frames go to the server, sends them, and turns what it
    sends back into results."""

    def __init__(self, client, *, send_distance, jpeg_quality):
        self.client = client
        self.send_distance = send_distance
        self.jpeg_quality = jpeg_quality
        self.last_sent_gps = None
        self.received = 0
        self.sent = 0
        self._last_received = 0
        self._last_stats = time.monotonic()

    def gated(self, gps):
        """Whether a frame at `gps` is held back: it has no fix, or is within
        send_distance of the last frame sent."""
        if self.send_distance <= 0:
            return False
        if gps is None:
            return True
        return (
            self.last_sent_gps is not None
            and haversine_m(self.last_sent_gps, gps) < self.send_distance
        )

    def on_input(self, input):
        """The result for an input: the next-flight area KML if the server
        planned one, empty otherwise."""
        self._log_stats()
        try:
            frame = read_frame(input.payload_type, bytes(input.data))
            gps = frame_gps(frame)
            if self.gated(gps):
                return b""
            if gps is None:
                # send_distance is 0, so frames without a fix are sent too
                gps = (math.nan, math.nan, math.nan)
            jpeg = to_jpeg(frame, self.jpeg_quality)
        except FrameError as err:
            logger.warning("input %d: %s", input.id.seq, err)
            return b""
        status, kml = self.client.send_frame(jpeg, gps)
        if status == ERROR:
            logger.warning("input %d: SwiftMap server unreachable; frame not mapped", input.id.seq)
            return b""
        self.last_sent_gps = gps
        self.sent += 1
        if kml:
            logger.info("input %d: SwiftMap planned a next-flight area (%d bytes of KML)",
                        input.id.seq, len(kml))
        return kml

    def _log_stats(self):
        self.received += 1
        now = time.monotonic()
        elapsed = now - self._last_stats
        if elapsed > STATS_INTERVAL_S:
            logger.info("%.2f fps (received %d, sent %d)",
                        (self.received - self._last_received) / elapsed, self.received, self.sent)
            self._last_received = self.received
            self._last_stats = now


def run(name, swiftmap, *, max_output_size, stop, on_ready=None):
    """Serves the engine `name` until the threading.Event `stop` is set. Each
    frame is forwarded on, with what `swiftmap` makes of it as the result."""
    try:
        with uriel.Engine(
            name, result_type=GENERIC_RESULT_TYPE, max_output_size=max_output_size
        ) as engine:
            logger.info("engine %s ready; mapping on %s:%s, one frame per %.1f m",
                        name, swiftmap.client.server, swiftmap.client.port,
                        swiftmap.send_distance)
            if on_ready is not None:
                on_ready()
            while not stop.is_set():
                input = engine.next(POLL_INTERVAL_S)
                if input is None:
                    continue
                try:
                    result = swiftmap.on_input(input)
                except Exception:
                    logger.exception("input %d: failed to handle it", input.id.seq)
                    result = b""
                if len(result) > max_output_size:
                    logger.warning("input %d: result is %d bytes, over max_output_size; "
                                   "not reporting it", input.id.seq, len(result))
                    result = b""
                engine.forward(result=result)
    finally:
        swiftmap.client.close()
