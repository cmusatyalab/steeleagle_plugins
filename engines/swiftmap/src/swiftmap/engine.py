"""The swiftmap engine: forwards each frame unchanged and sends it, with its
GPS fix, to the SwiftMap server in capture order. A gap in the JPEG encoder's
frame numbers is a lost frame: the engine asks reachback for it, holds the
frames after it, and sends them once it is back (as a file in the daemon's
files directory) or given up on. A planned next-flight KML is the next
frame's result, a GenericResult payload."""

import contextlib
import json
import logging
import math
import os
import queue
import re
import threading
import time

import cv2
import numpy as np
import uriel
from google.protobuf.message import DecodeError

from steeleagle_protocol.v1.messages.frame import frame_pb2
from swiftmap.client import ERROR
from swiftmap.inorder import InOrder

logger = logging.getLogger("swiftmap")

RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"
ENCODED_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/EncodedFrame"
GENERIC_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/GenericResult"
REQUEST_TYPE = "swiftmap/ReachbackRequest"
# A derived result must name a flow; reachback ignores it
REQUEST_FLOW = "swiftmap-reachback"
RGB_CHANNELS = 3
POLL_INTERVAL_S = 0.1
FILE_SCAN_INTERVAL_S = 0.5
STATS_INTERVAL_S = 5.0
NO_GPS = (math.nan, math.nan, math.nan)
FRAME_FILE = re.compile(r"reachback-(\d+)\.frame")


def frame_filename(number):
    return f"reachback-{number}.frame"


def encode_request(first, last):
    return json.dumps({"first": first, "last": last}).encode()


def decode_request(data):
    """(first, last) of a request; ValueError if it is not one."""
    try:
        request = json.loads(data)
        first, last = request["first"], request["last"]
    except (KeyError, TypeError) as err:
        raise ValueError(f"not a request: {err}") from None
    if not (isinstance(first, int) and isinstance(last, int) and 0 <= first <= last):
        raise ValueError(f"bad range {first!r} to {last!r}")
    return first, last


class FrameError(Exception):
    pass


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
    """The RawFrame or EncodedFrame `data`. Its type may be written with
    slashes or dots."""
    kind = payload_type.replace(".", "/")
    try:
        if kind == RAW_FRAME_TYPE:
            return frame_pb2.RawFrame.FromString(data)
        if kind == ENCODED_FRAME_TYPE:
            return frame_pb2.EncodedFrame.FromString(data)
    except DecodeError as err:
        raise FrameError(f"bad {payload_type}: {err}") from None
    raise FrameError(f"payload is a {payload_type!r}, not a RawFrame or EncodedFrame")


def rgb_pixels(frame):
    """A RawFrame's pixels, as a (height, width, 3) RGB array."""
    width, height = frame.h_res, frame.v_res
    if frame.channels != RGB_CHANNELS or len(frame.data) != width * height * RGB_CHANNELS:
        raise FrameError(
            f"{len(frame.data)} bytes is not a {width}x{height} frame of {frame.channels} channels"
        )
    return np.frombuffer(frame.data, dtype=np.uint8).reshape(height, width, RGB_CHANNELS)


def jpeg_bytes(frame):
    """An EncodedFrame's JPEG."""
    if frame.codec != frame_pb2.CODEC_JPEG:
        raise FrameError(f"{frame_pb2.Codec.Name(frame.codec)} frames are not supported, only JPEG")
    return frame.encoded_data


def to_jpeg(frame, quality):
    """The frame's image as a JPEG: an EncodedFrame's as it is, a RawFrame's
    RGB pixels encoded at `quality`."""
    if isinstance(frame, frame_pb2.EncodedFrame):
        return jpeg_bytes(frame)
    ok, jpeg = cv2.imencode(
        ".jpg", cv2.cvtColor(rgb_pixels(frame), cv2.COLOR_RGB2BGR),
        [cv2.IMWRITE_JPEG_QUALITY, quality],
    )
    if not ok:
        raise FrameError("failed to encode it as a JPEG")
    return jpeg.tobytes()


class Sender:
    """Sends frames to the server in order. Once entered, it sends on its own
    thread, so a burst of released frames doesn't block the engine."""

    def __init__(self, client):
        self.client = client
        self.sent = 0
        self._kml = b""
        self._lock = threading.Lock()
        self._queue = None

    def __enter__(self):
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="sender", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._queue.put(None)
        self._thread.join()
        self.client.close()

    def send(self, jpeg, gps):
        if self._queue is None:
            self._send(jpeg, gps)
        else:
            self._queue.put((jpeg, gps))

    def take_kml(self):
        """The KML planned since the last call, or b""."""
        with self._lock:
            kml, self._kml = self._kml, b""
        return kml

    def _run(self):
        while (item := self._queue.get()) is not None:
            try:
                self._send(*item)
            except Exception:
                logger.exception("failed to send a frame")

    def _send(self, jpeg, gps):
        status, kml = self.client.send_frame(jpeg, gps)
        if status == ERROR:
            logger.warning("SwiftMap server unreachable; frame not mapped")
            return
        self.sent += 1
        if kml:
            logger.info("SwiftMap planned a next-flight area (%d bytes of KML)", len(kml))
            with self._lock:
                self._kml = kml


class SwiftMap:
    """Sends frames in order with `sender`; a gap_timeout of 0 sends them as
    they come. Frames sent back arrive in `files_dir`."""

    def __init__(self, sender, *, jpeg_quality, gap_timeout, files_dir=None):
        self.sender = sender
        self.jpeg_quality = jpeg_quality
        self.inorder = InOrder(gap_timeout) if gap_timeout > 0 else None
        self.files_dir = files_dir
        self.stream = None  # (producer, session)
        self.last_id = None  # requests are about the last input
        self.received = 0
        self._last_scan = float("-inf")
        self._last_received = 0
        self._last_stats = time.monotonic()

    def _item(self, frame):
        return to_jpeg(frame, self.jpeg_quality), frame_gps(frame) or NO_GPS

    def _send(self, items):
        for item in items:
            self.sender.send(*item)

    def on_input(self, input, now):
        self._log_stats()
        try:
            frame = read_frame(input.payload_type, bytes(input.data))
            item = self._item(frame)
        except FrameError as err:
            logger.warning("input %d: %s", input.id.seq, err)
            return
        if self.inorder is None:
            self._send([item])
            return
        stream = (input.id.producer, input.id.session)
        if stream != self.stream:
            self._send(self.inorder.flush())
            self.stream = stream
        self.last_id = input.id
        missing = len(self.inorder.missing)
        self._send(self.inorder.add(frame.id, item, now))
        if len(self.inorder.missing) > missing:
            logger.info("frame %d: %d lost before it", frame.id, len(self.inorder.missing) - missing)

    def poll(self, now):
        """Takes in frames sent back and gives up on late ones; returns the
        requests to publish, as (input id, payload)."""
        if self.inorder is None:
            return []
        if self.files_dir and now - self._last_scan >= FILE_SCAN_INTERVAL_S:
            self._last_scan = now
            self._read_files()
        given_up = self.inorder.given_up
        self._send(self.inorder.expire(now))
        if self.inorder.given_up > given_up:
            logger.warning("gave up on %d lost frames", self.inorder.given_up - given_up)
        requests = self.inorder.requests(now)
        for first, last in requests:
            logger.info("asking reachback for frames %d to %d", first, last)
        return [(self.last_id, encode_request(first, last)) for first, last in requests]

    def _read_files(self):
        """Fills in missing frames from their files, and removes all frame files."""
        try:
            entries = list(os.scandir(self.files_dir))
        except OSError as err:
            logger.warning("can't read %s: %s", self.files_dir, err)
            return
        for entry in entries:
            match = FRAME_FILE.fullmatch(entry.name)
            if match is None:
                continue
            number = int(match[1])
            if number in self.inorder.missing:
                try:
                    with open(entry.path, "rb") as f:
                        frame = frame_pb2.EncodedFrame.FromString(f.read())
                    if frame.id != number:
                        raise FrameError(f"it holds frame {frame.id}")
                    self._send(self.inorder.fill(number, self._item(frame)))
                    logger.info("frame %d sent back", number)
                except (OSError, DecodeError, FrameError) as err:
                    logger.warning("can't read %s: %s", entry.name, err)
            with contextlib.suppress(OSError):
                os.remove(entry.path)

    def _log_stats(self):
        self.received += 1
        now = time.monotonic()
        elapsed = now - self._last_stats
        if elapsed <= STATS_INTERVAL_S:
            return
        extra = ""
        if self.inorder is not None:
            o = self.inorder
            extra = (f", {len(o.held)} held, {len(o.missing)} missing, "
                     f"{o.recovered} sent back, {o.given_up} given up")
        logger.info("%.2f fps (received %d, sent %d%s)",
                    (self.received - self._last_received) / elapsed, self.received,
                    self.sender.sent, extra)
        self._last_received, self._last_stats = self.received, now


def run(name, swiftmap, *, max_output_size, stop, on_ready=None):
    """Serves the engine `name` until `stop` is set."""
    with uriel.Engine(
        name, result_type=GENERIC_RESULT_TYPE, max_output_size=max_output_size
    ) as engine:
        client = swiftmap.sender.client
        logger.info("engine %s ready; mapping on %s:%s", name, client.server, client.port)
        if on_ready is not None:
            on_ready()
        while not stop.is_set():
            input = engine.next(POLL_INTERVAL_S)
            now = time.monotonic()
            if input is not None:
                try:
                    swiftmap.on_input(input, now)
                except Exception:
                    logger.exception("input %d: failed to handle it", input.id.seq)
                result = swiftmap.sender.take_kml()
                if len(result) > max_output_size:
                    logger.warning("input %d: KML of %d bytes is over max_output_size",
                                   input.id.seq, len(result))
                    result = b""
                engine.forward(result=result)
            try:
                for id, payload in swiftmap.poll(now):
                    engine.publish_derived(id, REQUEST_FLOW, payload, result_type=REQUEST_TYPE)
            except Exception:
                logger.exception("failed to handle the frames sent back")
