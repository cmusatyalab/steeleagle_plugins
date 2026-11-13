"""TCP client to a SwiftMap mapping server.

Wire: the client sends [BE uint32 size][JPEG][24-byte GPS][uint8 kind]: a new
frame, a frame sent back for the last plan, or, with no JPEG, the marker that
every frame sent back for it is in. The server replies with 3 float64 (status,
keyframe_count, total) followed by a big-endian uint32 length and an optional
trailing payload (a next-flight area KML; length 0 for an ordinary ack).
"""

import logging
import socket
import struct
import time

logger = logging.getLogger("swiftmap.client")

DEFAULT_PORT = 43322
SIZE_FMT = "!I"  # image-size header
GPS_FMT = "!3d"  # lat, lon, alt
KIND_FMT = "!B"
FRAME, REACHBACK, REACHBACK_DONE = 0, 1, 2
REPLY_FMT = "3d"  # status, keyframe_count, total_frames
REPLY_NBYTES = struct.calcsize(REPLY_FMT)
PAYLOAD_LEN_FMT = "!I"  # length of the trailing reply payload
PAYLOAD_LEN_NBYTES = struct.calcsize(PAYLOAD_LEN_FMT)
STATUS_KEYFRAME = 1.0

RETRY_INTERVAL_S = 5  # between blocking reconnect attempts
SOCKET_TIMEOUT_S = 15  # before a connect, send or recv is dead

KEYFRAME = "keyframe"
SKIPPED = "skipped"
ERROR = "error"


class SwiftMapClient:
    def __init__(self, server="localhost", port=DEFAULT_PORT):
        self.server = server
        self.port = port
        self.sock = None

    @property
    def connected(self):
        return self.sock is not None

    def _close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def connect(self, max_retries=None):
        """Connects, trying up to `max_retries` times (forever if None), and
        returns whether it did."""
        attempt = 0
        while max_retries is None or attempt < max_retries:
            attempt += 1
            self._close()
            try:
                self.sock = socket.create_connection(
                    (self.server, self.port), timeout=SOCKET_TIMEOUT_S
                )
                logger.info("connected to SwiftMap server at %s:%s", self.server, self.port)
                return True
            except OSError as err:
                logger.error("failed to connect to SwiftMap server at %s:%s: %s",
                             self.server, self.port, err)
                if max_retries is not None and attempt >= max_retries:
                    return False
                time.sleep(RETRY_INTERVAL_S)
        return False

    def _recv_exact(self, n):
        """Exactly `n` bytes, or None if the server closes early."""
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _recv_reply(self):
        """The reply's status and trailing payload, or None if the server
        closes early."""
        head = self._recv_exact(REPLY_NBYTES)
        if head is None:
            return None
        status, _keyframes, _total = struct.unpack(REPLY_FMT, head)
        len_buf = self._recv_exact(PAYLOAD_LEN_NBYTES)
        if len_buf is None:
            return None
        (n,) = struct.unpack(PAYLOAD_LEN_FMT, len_buf)
        payload = b""
        if n:
            payload = self._recv_exact(n)
            if payload is None:
                return None
        return status, payload

    def send_frame(self, jpeg, gps, kind=FRAME):
        """Sends the JPEG `jpeg` taken at `gps` (lat, lon, alt), a frame of
        `kind`, and returns (KEYFRAME, SKIPPED or ERROR, the KML the server
        sent back or b""). If the connection is dead, reconnects once and
        sends it again."""
        lat, lon, alt = gps
        payload = (struct.pack(SIZE_FMT, len(jpeg)) + jpeg + struct.pack(GPS_FMT, lat, lon, alt)
                   + struct.pack(KIND_FMT, kind))
        for attempt in range(2):
            if not self.connected and not self.connect(max_retries=1):
                return ERROR, b""
            try:
                self.sock.sendall(payload)
                reply = self._recv_reply()
                if reply is None:
                    raise ConnectionError("server closed the connection")
                status, kml = reply
                return (KEYFRAME if status == STATUS_KEYFRAME else SKIPPED), kml
            except OSError as err:
                retrying = " and sending the frame again" if attempt == 0 else ""
                logger.warning("SwiftMap connection lost (%s); reconnecting%s", err, retrying)
                self._close()
        return ERROR, b""

    def close(self):
        self._close()
