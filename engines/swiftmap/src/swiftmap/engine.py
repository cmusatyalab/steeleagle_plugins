"""The swiftmap engine: forwards each frame unchanged and sends it, with its
GPS fix, to the SwiftMap server as it arrives. A planned next-flight KML is
the next frame's result.

In NFN mode the server holds new frames from when a batch closes until its
rounds are done, and says so on its replies (NFN_HOLD, NFN_RELEASE); while
held, the engine polls it every POLL_S for its plans, since no new frames go
to it. Each plan with weak targets is a request, with an id of its own, for
frames already taken that cover them.

The engine tells the reachback engine the NFN status, whether the server
holds and the current request, at once when it changes and every STATUS_S
after, so a status lost on the way is soon made good. Reachback sends the
frames back in the flow, as REACHBACK_FRAME_TYPE inputs and then a
REACHBACK_DONE_TYPE, each stamped with the request it answers, and sends
again whatever does not arrive. The engine passes each on to the server once,
and drops anything for another request."""

import json
import logging
import math
import queue
import struct
import threading
import time
import xml.etree.ElementTree as ET
from typing import NamedTuple

import cv2
import numpy as np
import uriel
from google.protobuf.message import DecodeError

from steeleagle_protocol.v1.messages.frame import frame_pb2
from swiftmap.client import ERROR, FRAME, POLL, REACHBACK, REACHBACK_DONE

logger = logging.getLogger("swiftmap")

RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"
ENCODED_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/EncodedFrame"
REACHBACK_FRAME_TYPE = "swiftmap/ReachbackFrame"
REACHBACK_DONE_TYPE = "swiftmap/ReachbackDone"
GENERIC_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/GenericResult"
STATUS_TYPE = "swiftmap/NfnStatus"
RGB_CHANNELS = 3
POLL_INTERVAL_S = 0.1
STATS_INTERVAL_S = 5.0
POLL_S = 1.0
STATUS_S = 1.0
NO_GPS = (math.nan, math.nan, math.nan)
KML = "{http://www.opengis.net/kml/2.2}"
NFN_HOLD = b'{"nfn": "hold"}'
NFN_RELEASE = b'{"nfn": "release"}'
# Before each frame sent back: the request it answers and its number in the
# reply; before the end, the request and how many frames there are
STAMP_FMT = "!QI"
STAMP_NBYTES = struct.calcsize(STAMP_FMT)


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


class Request(NamedTuple):
    """A plan's request for `count` frames covering its `targets`, each
    [lat, lon, alt]. Its `id` is never reused, across restarts too."""

    id: int
    targets: list
    count: int


def encode_status(hold, request):
    """The NFN status: whether the server holds new frames, and the current
    Request or None."""
    return json.dumps(
        {"hold": hold, "request": None if request is None else request._asdict()}
    ).encode()


def decode_status(data):
    """(hold, Request or None) of an NFN status; ValueError if it is not one."""
    try:
        status = json.loads(data)
        hold, request = status["hold"], status["request"]
        if not isinstance(hold, bool):
            raise ValueError("hold is not true or false")
        if request is not None:
            request = Request(int(request["id"]),
                              [list(map(float, t)) for t in request["targets"]],
                              int(request["count"]))
    except (KeyError, TypeError, ValueError) as err:
        raise ValueError(f"not an NFN status: {err}") from None
    if request is not None and (request.id < 0 or request.count < 0
                                or any(len(t) != 3 for t in request.targets)):
        raise ValueError("a target is lat, lon, alt, and the id and count are not negative")
    return hold, request


class FrameError(Exception):
    pass


def stamp(request_id, number, data=b""):
    """`data` stamped with the request it answers and `number`: a frame's
    number in the reply, or on the end, how many frames there are."""
    return struct.pack(STAMP_FMT, request_id, number) + data


def unstamp(data):
    """(request id, number, data) of a stamped payload."""
    if len(data) < STAMP_NBYTES:
        raise FrameError(f"{len(data)} bytes is too short to be stamped")
    request_id, number = struct.unpack_from(STAMP_FMT, data)
    return request_id, number, data[STAMP_NBYTES:]


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
    thread, so the engine keeps taking frames while the server works."""

    def __init__(self, client):
        self.client = client
        self.sent = 0
        self.last_sent = time.monotonic()
        self._replies = []
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
        self.last_sent = time.monotonic()
        if self._queue is None:
            self._send(jpeg, gps, kind)
        else:
            self._queue.put((jpeg, gps, kind))

    def take_replies(self):
        """The payloads the server sent back since the last call, in order."""
        with self._lock:
            replies, self._replies = self._replies, []
        return replies

    def _run(self):
        while (item := self._queue.get()) is not None:
            try:
                self._send(*item)
            except Exception:
                logger.exception("failed to send a frame")

    def _send(self, jpeg, gps, kind):
        status, payload = self.client.send_frame(jpeg, gps, kind)
        if status == ERROR:
            logger.warning("SwiftMap server unreachable; frame not mapped")
            return
        self.sent += kind != POLL
        if payload:
            with self._lock:
                self._replies.append(payload)


class SwiftMap:
    """Sends frames to the server as they come, and frames sent back for the
    current request once each, marked as such. Keeps the NFN status: whether
    the server holds, which it says on its replies, and the request of its
    last plan. Polls the server while held."""

    def __init__(self, sender, *, jpeg_quality):
        self.sender = sender
        self.jpeg_quality = jpeg_quality
        # Whether the server has said anything of NFN yet; until then there
        # is no status to tell
        self.nfn = False
        self.holding = False
        self.request = None
        self.last_id = None
        self.result = b""
        self.received = 0
        # Request ids go up from the time the engine started, so a restart
        # never reuses one reachback has seen
        self._last_request_id = time.time_ns()
        self._sent_back = set()
        self._done = False
        self._status_due = False
        self._last_status = float("-inf")
        self._last_received = 0
        self._last_stats = time.monotonic()

    def on_input(self, input):
        self._log_stats()
        self.last_id = input.id
        kind = input.payload_type.replace(".", "/")
        try:
            if kind == REACHBACK_DONE_TYPE:
                request_id, count, _ = unstamp(bytes(input.data))
                self._on_done(request_id, count)
            elif kind == REACHBACK_FRAME_TYPE:
                self._on_sent_back(*unstamp(bytes(input.data)))
            else:
                frame = read_frame(input.payload_type, bytes(input.data))
                self.sender.send(to_jpeg(frame, self.jpeg_quality), frame_gps(frame) or NO_GPS,
                                 FRAME)
        except FrameError as err:
            logger.warning("input %d: %s", input.id.seq, err)

    def _current(self, request_id):
        """Whether `request_id` is the current request, whose end is not in."""
        return self.request is not None and request_id == self.request.id and not self._done

    def _on_sent_back(self, request_id, number, data):
        if not self._current(request_id) or number in self._sent_back:
            logger.info("dropping frame %d of request %d: sent twice, late, or for an old request",
                        number, request_id)
            return
        frame = read_frame(ENCODED_FRAME_TYPE, data)
        self.sender.send(to_jpeg(frame, self.jpeg_quality), frame_gps(frame) or NO_GPS, REACHBACK)
        self._sent_back.add(number)

    def _on_done(self, request_id, count):
        if not self._current(request_id):
            logger.info("dropping the end of request %d: sent twice or for an old request",
                        request_id)
            return
        self._done = True
        logger.info("request %d: all frames sent back are in, %d of the %d picked",
                    request_id, len(self._sent_back), count)
        self.sender.send(b"", NO_GPS, REACHBACK_DONE)

    def on_reply(self, payload):
        """Handles a payload the server sent back: an NFN hold or release, or a
        plan, the next result, which is the next request if it has weak
        targets and a count."""
        if payload in (NFN_HOLD, NFN_RELEASE):
            self.nfn = True
            self.holding = payload == NFN_HOLD
            logger.info("NFN %s", "hold: polling for plans" if self.holding else "release")
            self._set_request(None)
            return
        logger.info("SwiftMap planned a next-flight area (%d bytes of KML)", len(payload))
        self.result = payload
        try:
            targets, count = read_plan(payload)
        except ValueError as err:
            logger.warning("can't read the planned KML: %s", err)
            targets, count = [], 0
        if not (targets and count):
            self._set_request(None)
            return
        self.nfn = True
        self._last_request_id += 1
        logger.info("request %d: %d frames covering %d weak targets",
                    self._last_request_id, count, len(targets))
        self._set_request(Request(self._last_request_id, targets, count))

    def _set_request(self, request):
        self.request = request
        self._sent_back = set()
        self._done = False
        self._status_due = True

    def take_status(self, now):
        """The NFN status to tell reachback, or None if none is due: one is at
        once when it changes, and every STATUS_S from the server's first NFN
        notice or plan on."""
        if not self.nfn or self.last_id is None:
            return None
        if not self._status_due and now - self._last_status < STATUS_S:
            return None
        self._status_due = False
        self._last_status = now
        return encode_status(self.holding, self.request)

    def take_result(self):
        """The plan to report as the next result, or b""."""
        result, self.result = self.result, b""
        return result

    def poll(self, now):
        """While held, asks the server every POLL_S for what it has queued."""
        if self.holding and now - self.sender.last_sent >= POLL_S:
            self.sender.send(b"", NO_GPS, POLL)

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
            for payload in swiftmap.sender.take_replies():
                try:
                    swiftmap.on_reply(payload)
                except Exception:
                    logger.exception("failed to handle what the server sent back")
            if input is not None:
                result = swiftmap.take_result()
                if len(result) > max_output_size:
                    logger.warning("input %d: KML of %d bytes is over max_output_size",
                                   input.id.seq, len(result))
                    result = b""
                engine.forward(result=result)
            swiftmap.poll(time.monotonic())
            if flow is not None and (status := swiftmap.take_status(time.monotonic())) is not None:
                try:
                    engine.publish_derived(swiftmap.last_id, flow, status, result_type=STATUS_TYPE)
                except Exception:
                    logger.exception("failed to tell reachback the NFN status")
