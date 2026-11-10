import socket
import struct
import threading
from dataclasses import dataclass

import pytest

from steeleagle_protocol.v1.common import common_pb2
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.telemetry import telemetry_pb2
from swiftmap.client import GPS_FMT, PAYLOAD_LEN_FMT, REPLY_FMT, SIZE_FMT
from swiftmap.engine import ENCODED_FRAME_TYPE


class FakeServer:
    """A stand-in SwiftMap server. It accepts one connection at a time,
    records each (jpeg, gps) it gets, and replies with a keyframe status and
    the KML queued for it, if any."""

    def __init__(self):
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.received = []
        self.replies = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _recv_exact(self, conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            with conn:
                while (head := self._recv_exact(conn, 4)) is not None:
                    (n,) = struct.unpack(SIZE_FMT, head)
                    jpeg = self._recv_exact(conn, n)
                    gps = struct.unpack(GPS_FMT, self._recv_exact(conn, 24))
                    self.received.append((jpeg, gps))
                    kml = self.replies.pop(0) if self.replies else b""
                    conn.sendall(
                        struct.pack(REPLY_FMT, 1.0, len(self.received), len(self.received))
                        + struct.pack(PAYLOAD_LEN_FMT, len(kml))
                        + kml
                    )

    def close(self):
        self.listener.close()


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.close()


@dataclass
class Id:
    seq: int
    producer: str = "camera"
    session: int = 1


@dataclass
class Input:
    payload_type: str
    data: bytes
    id: Id


def position(lat, lon, alt=10.0):
    return telemetry_pb2.PositionInfo(
        global_position=common_pb2.GlobalPosition(latitude=lat, longitude=lon, altitude=alt)
    )


def numbered_frame(number):
    """A JPEG EncodedFrame numbered `number` as the JPEG encoder numbers them,
    whose "JPEG" says its number, so the server shows the order frames came."""
    return frame_pb2.EncodedFrame(
        encoded_data=f"jpeg-{number}".encode(), id=number, codec=frame_pb2.CODEC_JPEG,
        position_info=position(40.0, -79.0),
    )


def numbered_input(number, seq=None, **id):
    frame = numbered_frame(number)
    return Input(ENCODED_FRAME_TYPE, frame.SerializeToString(), Id(number if seq is None else seq, **id))


def numbers_received(server):
    """The numbers of the numbered frames the server got, in order."""
    return [int(jpeg.decode().removeprefix("jpeg-")) for jpeg, _ in server.received]
