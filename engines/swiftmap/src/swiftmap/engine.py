"""The swiftmap engine: forwards each frame unchanged and sends it, with its
GPS fix, to the SwiftMap server as it arrives. A planned next-flight KML is
the next frame's result; the weakly reconstructed targets its viewpoints look
at ask reachback for the frames already taken that cover them. They come back
as files in the daemon's files directory and are sent too."""

import contextlib
import json
import logging
import math
import os
import queue
import re
import threading
import time
import xml.etree.ElementTree as ET

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
REQUEST_TYPE = "swiftmap/ReachbackRequest"
RGB_CHANNELS = 3
POLL_INTERVAL_S = 0.1
FILE_SCAN_INTERVAL_S = 0.5
STATS_INTERVAL_S = 5.0
NO_GPS = (math.nan, math.nan, math.nan)
FRAME_FILE = re.compile(r"reachback-(\d+)\.frame")
KML = "{http://www.opengis.net/kml/2.2}"
KEEP_SENT = 5000


def frame_filename(number):
    return f"reachback-{number}.frame"


def targets(kml):
    """[lat, lon, alt] of the target each viewpoint in `kml` looks at;
    ValueError if it is not KML."""
    try:
        root = ET.fromstring(kml)
    except ET.ParseError as err:
        raise ValueError(f"not KML: {err}") from None
    return [[float(look.findtext(f"{KML}{k}")) for k in ("latitude", "longitude", "altitude")]
            for look in root.iter(f"{KML}LookAt")]


def encode_request(targets, exclude):
    return json.dumps({"targets": targets, "exclude": sorted(exclude)}).encode()


def decode_request(data):
    """(targets, exclude) of a request; ValueError if it is not one."""
    try:
        request = json.loads(data)
        found = [list(map(float, t)) for t in request["targets"]]
        exclude = set(map(int, request["exclude"]))
    except (KeyError, TypeError, ValueError) as err:
        raise ValueError(f"not a request: {err}") from None
    if any(len(t) != 3 for t in found):
        raise ValueError("a target is lat, lon, alt")
    return found, exclude


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
    thread, so a burst of frames sent back doesn't block the engine."""

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
    """Sends frames to the server as they come, and asks reachback for the
    frames covering each weak target the server plans for. Frames sent back
    arrive in `files_dir`. It follows one camera at a time."""

    def __init__(self, sender, *, jpeg_quality, files_dir=None):
        self.sender = sender
        self.jpeg_quality = jpeg_quality
        self.files_dir = files_dir
        self.stream = None
        self.sent = {}
        self.requests = []
        self.received = self.sent_back = 0
        self._last_scan = float("-inf")
        self._last_received = 0
        self._last_stats = time.monotonic()

    def _send(self, frame):
        self.sender.send(to_jpeg(frame, self.jpeg_quality), frame_gps(frame) or NO_GPS)
        self.sent[frame.id] = None
        if len(self.sent) > KEEP_SENT:
            del self.sent[next(iter(self.sent))]

    def on_input(self, input):
        self._log_stats()
        stream = (input.id.producer, input.id.session)
        if stream != self.stream:
            self.stream, self.sent = stream, {}
        try:
            self._send(read_frame(input.payload_type, bytes(input.data)))
        except FrameError as err:
            logger.warning("input %d: %s", input.id.seq, err)

    def on_kml(self, id, kml):
        """Asks for the frames covering the targets in `kml`, about the input `id`."""
        try:
            found = targets(kml)
        except ValueError as err:
            logger.warning("can't read the planned KML: %s", err)
            return
        if found:
            logger.info("asking for the frames covering %d weak targets", len(found))
            self.requests.append((id, encode_request(found, self.sent)))

    def take_requests(self):
        """The requests to publish, as (input id, payload)."""
        requests, self.requests = self.requests, []
        return requests

    def poll(self, now):
        """Sends the frames sent back since the last scan."""
        if self.files_dir and now - self._last_scan >= FILE_SCAN_INTERVAL_S:
            self._last_scan = now
            self._read_files()

    def _read_files(self):
        try:
            entries = list(os.scandir(self.files_dir))
        except OSError as err:
            logger.warning("can't read %s: %s", self.files_dir, err)
            return
        frames = []
        for entry in entries:
            match = FRAME_FILE.fullmatch(entry.name)
            if match is None:
                continue
            try:
                with open(entry.path, "rb") as f:
                    frame = frame_pb2.EncodedFrame.FromString(f.read())
                if frame.id != int(match[1]):
                    raise FrameError(f"it holds frame {frame.id}")
                frames.append(frame)
            except (OSError, DecodeError, FrameError) as err:
                logger.warning("can't read %s: %s", entry.name, err)
            with contextlib.suppress(OSError):
                os.remove(entry.path)
        for frame in sorted(frames, key=lambda f: f.id):
            if frame.id not in self.sent:
                self._send(frame)
                self.sent_back += 1
                logger.info("frame %d sent back", frame.id)

    def _log_stats(self):
        self.received += 1
        now = time.monotonic()
        elapsed = now - self._last_stats
        if elapsed <= STATS_INTERVAL_S:
            return
        logger.info("%.2f fps (received %d, sent %d, %d sent back)",
                    (self.received - self._last_received) / elapsed, self.received,
                    self.sender.sent, self.sent_back)
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
            try:
                swiftmap.poll(time.monotonic())
                if flow is not None:
                    for id, payload in swiftmap.take_requests():
                        engine.publish_derived(id, flow, payload, result_type=REQUEST_TYPE)
            except Exception:
                logger.exception("failed to handle the frames sent back")
