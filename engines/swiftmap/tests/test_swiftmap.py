"""Tests of the swiftmap engine against a stand-in SwiftMap server."""

import math
import socket
import struct
import threading
from dataclasses import dataclass

import cv2
import numpy as np
import pytest

from steeleagle_protocol.v1.common import common_pb2
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.telemetry import telemetry_pb2
from swiftmap.client import GPS_FMT, PAYLOAD_LEN_FMT, REPLY_FMT, SIZE_FMT, SwiftMapClient
from swiftmap.engine import ENCODED_FRAME_TYPE, RAW_FRAME_TYPE, SwiftMap
from swiftmap.main import SWIFTMAP_PLUGIN, ConfigError, parse_args

KML = b"<kml>next flight</kml>"


class FakeServer:
    """Accepts one connection at a time, records each (jpeg, gps) it gets, and
    replies with a keyframe status and the KML queued for it, if any."""

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


def raw_input(seq, gps=None, width=32, height=24):
    frame = frame_pb2.RawFrame(
        data=np.full((height, width, 3), 120, dtype=np.uint8).tobytes(),
        h_res=width, v_res=height, channels=3, id=seq,
    )
    if gps is not None:
        frame.position_info.CopyFrom(position(*gps))
    return Input(RAW_FRAME_TYPE, frame.SerializeToString(), Id(seq))


def jpeg_input(seq, gps, codec=frame_pb2.CODEC_JPEG):
    jpeg = cv2.imencode(".jpg", np.zeros((8, 8, 3), dtype=np.uint8))[1].tobytes()
    frame = frame_pb2.EncodedFrame(
        encoded_data=jpeg, id=seq, codec=codec, position_info=position(*gps)
    )
    return Input(ENCODED_FRAME_TYPE, frame.SerializeToString(), Id(seq)), jpeg


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.close()


def swiftmap(server):
    return SwiftMap(SwiftMapClient("127.0.0.1", server.port), jpeg_quality=90)


def test_raw_frames_go_to_the_server_as_jpegs_with_their_gps(server):
    engine = swiftmap(server)
    assert engine.on_input(raw_input(1, (40.0, -79.0, 12.0))) == b""
    [(jpeg, gps)] = server.received
    assert gps == (40.0, -79.0, 12.0)
    assert cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR).shape == (24, 32, 3)


def test_jpeg_frames_go_to_the_server_as_they_are(server):
    input, jpeg = jpeg_input(1, (40.0, -79.0))
    swiftmap(server).on_input(input)
    assert server.received[0][0] == jpeg


def test_h264_frames_are_not_sent(server):
    input, _ = jpeg_input(1, (40.0, -79.0), codec=frame_pb2.CODEC_H264)
    assert swiftmap(server).on_input(input) == b""
    assert server.received == []


def test_a_planned_area_is_the_result(server):
    server.replies.append(KML)
    assert swiftmap(server).on_input(raw_input(1, (40.0, -79.0))) == KML


def test_every_frame_is_sent_however_close_together(server):
    # Choosing frames is the keyframe engine's job, in front of this one
    engine = swiftmap(server)
    for seq in (1, 2):
        engine.on_input(raw_input(seq, (40.0, -79.0)))
    assert len(server.received) == 2


def test_a_frame_without_a_fix_is_sent_with_a_nan_one(server):
    swiftmap(server).on_input(raw_input(1))
    [(_, gps)] = server.received
    assert all(math.isnan(v) for v in gps)


def test_the_engine_reconnects_when_the_server_drops_the_connection(server):
    engine = swiftmap(server)
    engine.on_input(raw_input(1, (40.0, -79.0)))
    engine.client.sock.shutdown(socket.SHUT_RDWR)
    engine.on_input(raw_input(2, (41.0, -79.0)))
    assert len(server.received) == 2


def test_an_unreachable_server_gives_no_result():
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    engine = SwiftMap(SwiftMapClient("127.0.0.1", port), jpeg_quality=90)
    assert engine.on_input(raw_input(1, (40.0, -79.0))) == b""
    assert engine.sent == 0


def test_args_are_defaulted_and_checked():
    args = parse_args(SWIFTMAP_PLUGIN, '{"server": "mapper"}')
    assert (args["server"], args["server_port"], args["jpeg_quality"]) == ("mapper", 43322, 95)
    with pytest.raises(ConfigError):
        parse_args(SWIFTMAP_PLUGIN, '{"server": ""}')
    with pytest.raises(ConfigError):
        parse_args(SWIFTMAP_PLUGIN, '{"jpeg_quality": 0}')
