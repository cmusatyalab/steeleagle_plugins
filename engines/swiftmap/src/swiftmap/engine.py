"""The swiftmap engine: forwards each frame unchanged and sends it, with its
GPS fix, to the SwiftMap server as it arrives. A planned next-flight KML is
the next frame's result, and asks the reachback engine for frames already
taken that cover the weak targets its viewpoints look at. Those come back in
the flow, as REACHBACK_FRAME_TYPE inputs and then a REACHBACK_DONE_TYPE, and
go to the server marked as such."""

import json
import logging
import math
import queue
import threading
import time
import xml.etree.ElementTree as ET

import cv2
import numpy as np
import uriel
from google.protobuf.message import DecodeError

from steeleagle_protocol.v1.messages.frame import frame_pb2
from swiftmap.client import ERROR, FRAME, REACHBACK, REACHBACK_DONE

logger = logging.getLogger("swiftmap")

RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"
ENCODED_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/EncodedFrame"
REACHBACK_FRAME_TYPE = "swiftmap/ReachbackFrame"
REACHBACK_DONE_TYPE = "swiftmap/ReachbackDone"
GENERIC_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/GenericResult"
REQUEST_TYPE = "swiftmap/ReachbackRequest"
RGB_CHANNELS = 3
POLL_INTERVAL_S = 0.1
STATS_INTERVAL_S = 5.0
NO_GPS = (math.nan, math.nan, math.nan)
KML = "{http://www.opengis.net/kml/2.2}"


def read_plan(kml):
    """([lat, lon, alt] of the target each viewpoint looks at, how many frames
    to send back for them) of the plan `kml`; ValueError if it is not KML."""
    try:
        root = ET.fromstring(kml)
    except ET.ParseError as err:
        raise ValueError(f"not KML: {err}") from None
    targets = [[float(look.findtext(f"{KML}{k}")) for k in ("latitude", "longitude", "altitude")]
               for look in root.iter(f"{KML}LookAt")]
    count = next((int(data.findtext(f"{KML}value")) for data in root.iter(f"{KML}Data")
                  if data.get("name") == "reachback"), 0)
    return targets, count


def encode_request(targets, count):
    return json.dumps({"targets": targets, "count": count}).encode()


def decode_request(data):
    """(targets, count) of a request; ValueError if it is not one."""
    try:
        request = json.loads(data)
        targets = [list(map(float, t)) for t in request["targets"]]
        count = int(request["count"])
    except (KeyError, TypeError, ValueError) as err:
        raise ValueError(f"not a request: {err}") from None
    if any(len(t) != 3 for t in targets) or count < 0:
        raise ValueError("a target is lat, lon, alt, and the count is not negative")
    return targets, count


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
    slashes or dots; a frame sent back is an EncodedFrame."""
    kind = payload_type.replace(".", "/")
    try:
        if kind == RAW_FRAME_TYPE:
            return frame_pb2.RawFrame.FromString(data)
        if kind in (ENCODED_FRAME_TYPE, REACHBACK_FRAME_TYPE):
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
    thread, so the engine keeps taking frames while the server works."""

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

    def send(self, jpeg, gps, kind=FRAME):
        if self._queue is None:
            self._send(jpeg, gps, kind)
        else:
            self._queue.put((jpeg, gps, kind))

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

    def _send(self, jpeg, gps, kind):
        status, kml = self.client.send_frame(jpeg, gps, kind)
        if status == ERROR:
            logger.warning("SwiftMap server unreachable; frame not mapped")
            return
        self.sent += 1
        if kml:
            logger.info("SwiftMap planned a next-flight area (%d bytes of KML)", len(kml))
            with self._lock:
                self._kml = kml


class SwiftMap:
    """Sends frames to the server as they come, frames sent back marked as
    such, and asks reachback for the frames covering the weak targets of each
    plan the server sends."""

    def __init__(self, sender, *, jpeg_quality):
        self.sender = sender
        self.jpeg_quality = jpeg_quality
        self.requests = []
        self.received = self.sent_back = 0
        self._last_received = 0
        self._last_stats = time.monotonic()

    def on_input(self, input):
        self._log_stats()
        kind = input.payload_type.replace(".", "/")
        if kind == REACHBACK_DONE_TYPE:
            logger.info("all %d frames sent back are in", self.sent_back)
            self.sender.send(b"", NO_GPS, REACHBACK_DONE)
            self.sent_back = 0
            return
        try:
            frame = read_frame(input.payload_type, bytes(input.data))
            reachback = kind == REACHBACK_FRAME_TYPE
            self.sender.send(to_jpeg(frame, self.jpeg_quality), frame_gps(frame) or NO_GPS,
                             REACHBACK if reachback else FRAME)
            self.sent_back += reachback
        except FrameError as err:
            logger.warning("input %d: %s", input.id.seq, err)

    def on_kml(self, id, kml):
        """Asks for the frames covering the targets of the plan `kml`, about the input `id`."""
        try:
            targets, count = read_plan(kml)
        except ValueError as err:
            logger.warning("can't read the planned KML: %s", err)
            return
        if targets and count:
            logger.info("asking for %d frames covering %d weak targets", count, len(targets))
            self.requests.append((id, encode_request(targets, count)))

    def take_requests(self):
        """The requests to publish, as (input id, payload)."""
        requests, self.requests = self.requests, []
        return requests

    def _log_stats(self):
        self.received += 1
        now = time.monotonic()
        elapsed = now - self._last_stats
        if elapsed <= STATS_INTERVAL_S:
            return
        logger.info("%.2f fps (received %d, sent %d)",
                    (self.received - self._last_received) / elapsed, self.received,
                    self.sender.sent)
        self._last_received, self._last_stats = self.received, now


def run(name, swiftmap, *, max_output_size, stop, on_ready=None):
    """Serves the engine `name` until `stop` is set."""
    with uriel.Engine(
        name, result_type=GENERIC_RESULT_TYPE, max_output_size=max_output_size
    ) as engine:
        engine.subscribe(name)
        flow = None
        client = swiftmap.sender.client
        logger.info("engine %s ready; mapping on %s:%s", name, client.server, client.port)
        if on_ready is not None:
            on_ready()
        while not stop.is_set():
            engine.wait(POLL_INTERVAL_S)
            while (event := engine.poll_event()) is not None:
                flow = event.flow or flow
            input = engine.poll()
            if input is not None:
                try:
                    swiftmap.on_input(input)
                except Exception:
                    logger.exception("input %d: failed to handle it", input.id.seq)
                result = swiftmap.sender.take_kml()
                if result:
                    swiftmap.on_kml(input.id, result)
                if len(result) > max_output_size:
                    logger.warning("input %d: KML of %d bytes is over max_output_size",
                                   input.id.seq, len(result))
                    result = b""
                engine.forward(result=result)
            if flow is not None:
                for id, payload in swiftmap.take_requests():
                    try:
                        engine.publish_derived(id, flow, payload, result_type=REQUEST_TYPE)
                    except Exception:
                        logger.exception("failed to ask reachback for frames")
