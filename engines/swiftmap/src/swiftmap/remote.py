"""The swiftmap engine, on the remote daemon: forwards each frame unchanged and
sends its WebP and GPS to the SwiftMap server over TCP. The server's next-flight
KML is the next frame's result, and its weak targets the next reachback request.

In NFN mode the server holds new frames while a batch's rounds run (NFN_HOLD,
NFN_RELEASE on its replies); while held the engine polls it every POLL_S. It
tells reachback the NFN status when it changes and every STATUS_S, and passes
each frame sent back, then the end, to the server once.

Wire: [BE uint32 size][WebP][3 x BE float64 lat, lon, alt][uint8 kind]; no image
for the end marker and polls. Reply: 3 native float64 (status, keyframes,
total), a BE uint32 length, then that payload (KML or an NFN notice)."""

import logging
import math
import queue
import socket
import struct
import threading
import time
import xml.etree.ElementTree as ET

import uriel

from swiftmap.protocol import (
    ENCODED_FRAME_TYPE, GENERIC_RESULT_TYPE, POLL_INTERVAL_S, REACHBACK_DONE_TYPE,
    REACHBACK_FRAME_TYPE, STATS_INTERVAL_S, STATUS_TYPE, FrameError, Request, encode_status,
    frame_gps, read_frame, same_type, unstamp, webp_bytes,
)

logger = logging.getLogger("swiftmap")

DEFAULT_PORT = 43322
SIZE_FMT, GPS_FMT, KIND_FMT = "!I", "!3d", "!B"
REPLY_FMT, PAYLOAD_LEN_FMT = "3d", "!I"
FRAME, REACHBACK, REACHBACK_DONE, POLL = 0, 1, 2, 3
KEYFRAME, SKIPPED, ERROR = "keyframe", "skipped", "error"
RETRY_INTERVAL_S = 5
SOCKET_TIMEOUT_S = 15
POLL_S = STATUS_S = 1.0
NO_GPS = (math.nan, math.nan, math.nan)
KML = "{http://www.opengis.net/kml/2.2}"
NFN_HOLD = b'{"nfn": "hold"}'
NFN_RELEASE = b'{"nfn": "release"}'


def read_plan(kml):
    """([lat, lon, alt] each viewpoint looks at, frames to send back for them);
    ValueError if `kml` is not KML."""
    try:
        root = ET.fromstring(kml)
    except ET.ParseError as err:
        raise ValueError(f"not KML: {err}") from None
    targets = [[float(look.findtext(f"{KML}{k}")) for k in ("latitude", "longitude", "altitude")]
               for look in root.iter(f"{KML}LookAt")]
    count = next((int(d.findtext(f"{KML}value")) for d in root.iter(f"{KML}Data")
                  if d.get("name") == "reachback"), 0)
    return targets, count


class SwiftMapClient:
    """TCP client to a SwiftMap server."""

    def __init__(self, server="localhost", port=DEFAULT_PORT):
        self.server = server
        self.port = port
        self.sock = None

    @property
    def connected(self):
        return self.sock is not None

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def connect(self, max_retries=None):
        """Tries up to `max_retries` times (forever if None); whether it connected."""
        attempt = 0
        while max_retries is None or attempt < max_retries:
            attempt += 1
            self.close()
            try:
                self.sock = socket.create_connection((self.server, self.port), timeout=SOCKET_TIMEOUT_S)
                logger.info("connected to SwiftMap server at %s:%s", self.server, self.port)
                return True
            except OSError as err:
                logger.error("failed to connect to SwiftMap server at %s:%s: %s",
                             self.server, self.port, err)
                if max_retries is not None and attempt >= max_retries:
                    return False
                time.sleep(RETRY_INTERVAL_S)
        return False

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            if not (chunk := self.sock.recv(n - len(buf))):
                raise ConnectionError("server closed the connection")
            buf += chunk
        return buf

    def send_frame(self, image, gps, kind=FRAME):
        """Sends `image` taken at `gps`; (KEYFRAME, SKIPPED or ERROR, the payload
        sent back or b""). Reconnects and resends once if the connection is dead."""
        message = (struct.pack(SIZE_FMT, len(image)) + image + struct.pack(GPS_FMT, *gps)
                   + struct.pack(KIND_FMT, kind))
        for attempt in range(2):
            if not self.connected and not self.connect(max_retries=1):
                return ERROR, b""
            try:
                self.sock.sendall(message)
                status, _, _ = struct.unpack(REPLY_FMT, self._recv(struct.calcsize(REPLY_FMT)))
                (n,) = struct.unpack(PAYLOAD_LEN_FMT, self._recv(struct.calcsize(PAYLOAD_LEN_FMT)))
                return (KEYFRAME if status == 1.0 else SKIPPED), self._recv(n) if n else b""
            except OSError as err:
                logger.warning("SwiftMap connection lost (%s); reconnecting%s", err,
                               " and resending" if attempt == 0 else "")
                self.close()
        return ERROR, b""


class Sender:
    """Sends frames to the server in order; once entered, on its own thread."""

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

    def send(self, image, gps, kind=FRAME):
        self.last_sent = time.monotonic()
        if self._queue is None:
            self._send(image, gps, kind)
        else:
            self._queue.put((image, gps, kind))

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

    def _send(self, image, gps, kind):
        status, payload = self.client.send_frame(image, gps, kind)
        if status == ERROR:
            logger.warning("SwiftMap server unreachable; frame not mapped")
            return
        self.sent += kind != POLL
        if payload:
            with self._lock:
                self._replies.append(payload)


class SwiftMap:
    """Sends frames to the server, and the current request's frames sent back
    once each; keeps the NFN status (hold, request) and polls while held."""

    def __init__(self, sender):
        self.sender = sender
        self.nfn = False  # no status to tell until the server speaks NFN
        self.holding = False
        self.request = None
        self.last_id = None
        self.result = b""
        self.received = 0
        self._last_request_id = time.time_ns()  # never reused across restarts
        self._sent_back = set()
        self._done = False
        self._status_due = False
        self._last_status = float("-inf")
        self._last_stats = (0, time.monotonic())

    def on_input(self, input):
        self._log_stats()
        self.last_id = input.id
        try:
            if same_type(input.payload_type, REACHBACK_DONE_TYPE):
                request_id, count, _ = unstamp(bytes(input.data))
                self._on_done(request_id, count)
            elif same_type(input.payload_type, REACHBACK_FRAME_TYPE):
                self._on_sent_back(*unstamp(bytes(input.data)))
            else:
                frame = read_frame(input.payload_type, bytes(input.data))
                self.sender.send(webp_bytes(frame), frame_gps(frame) or NO_GPS, FRAME)
        except FrameError as err:
            logger.warning("input %d: %s", input.id.seq, err)

    def _current(self, request_id):
        return self.request is not None and request_id == self.request.id and not self._done

    def _on_sent_back(self, request_id, number, data):
        if not self._current(request_id) or number in self._sent_back:
            logger.info("dropping frame %d of request %d: duplicate, late or old", number, request_id)
            return
        frame = read_frame(ENCODED_FRAME_TYPE, data)
        self.sender.send(webp_bytes(frame), frame_gps(frame) or NO_GPS, REACHBACK)
        self._sent_back.add(number)

    def _on_done(self, request_id, count):
        if not self._current(request_id):
            logger.info("dropping the end of request %d: duplicate or old", request_id)
            return
        self._done = True
        logger.info("request %d: all frames sent back are in, %d of the %d picked",
                    request_id, len(self._sent_back), count)
        self.sender.send(b"", NO_GPS, REACHBACK_DONE)

    def on_reply(self, payload):
        """An NFN notice, or a plan: the next result, and a request if it has
        weak targets and a count."""
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
        """The NFN status to tell reachback when it changed or STATUS_S passed; else None."""
        if not self.nfn or self.last_id is None:
            return None
        if not self._status_due and now - self._last_status < STATUS_S:
            return None
        self._status_due = False
        self._last_status = now
        return encode_status(self.holding, self.request)

    def take_result(self):
        result, self.result = self.result, b""
        return result

    def poll(self, now):
        if self.holding and now - self.sender.last_sent >= POLL_S:
            self.sender.send(b"", NO_GPS, POLL)

    def _log_stats(self):
        self.received += 1
        received, since = self._last_stats
        elapsed = time.monotonic() - since
        if elapsed > STATS_INTERVAL_S:
            logger.info("%.2f fps (received %d, sent %d)",
                        (self.received - received) / elapsed, self.received, self.sender.sent)
            self._last_stats = (self.received, time.monotonic())


def run(name, swiftmap, *, max_output_size, stop, on_ready=None):
    """Serves the engine `name` until `stop` is set."""
    with uriel.Engine(name, result_type=GENERIC_RESULT_TYPE, max_output_size=max_output_size) as engine:
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
            if (input := engine.poll()) is not None:
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
