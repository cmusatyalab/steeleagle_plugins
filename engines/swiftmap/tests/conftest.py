import math
import socket
import struct
import threading
from dataclasses import dataclass

import pytest

from steeleagle_protocol.v1.common import common_pb2
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.telemetry import telemetry_pb2
from swiftmap.client import GPS_FMT, KIND_FMT, PAYLOAD_LEN_FMT, REPLY_FMT, SIZE_FMT
from swiftmap.engine import ENCODED_FRAME_TYPE


class FakeServer:
    """A stand-in SwiftMap server. It accepts one connection at a time,
    records each (jpeg, gps) it gets and the kind of every message, and
    replies with a keyframe status and the KML queued for it, if any."""

    def __init__(self):
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.received = []
        self.kinds = []
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
                    (kind,) = struct.unpack(KIND_FMT, self._recv_exact(conn, 1))
                    self.kinds.append(kind)
                    if jpeg:
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


@dataclass(frozen=True)
class Id:
    """An input id; hashable, as uriel's is, since reachback keys sends by it."""

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


TARGET = (40.0, -79.0, 0.0)


def near(east, north, up, origin=TARGET):
    """(lat, lon, alt) `east`, `north` and `up` metres from `origin`."""
    lat, lon, alt = origin
    return (lat + north / 111_320.0, lon + east / (111_320.0 * math.cos(math.radians(lat))),
            alt + up)


def numbered_frame(number, gps=(40.0, -79.0, 10.0), yaw=0.0, pitch=-90.0):
    """A JPEG EncodedFrame numbered `number` as the JPEG encoder numbers them,
    whose "JPEG" says its number, so the server shows the order frames came,
    taken at `gps` with the camera at `yaw` and `pitch` degrees."""
    return frame_pb2.EncodedFrame(
        encoded_data=f"jpeg-{number}".encode(), id=number, codec=frame_pb2.CODEC_JPEG,
        position_info=position(*gps),
        gimbal_info=telemetry_pb2.GimbalInfo(pose_neu=common_pb2.Pose(yaw=yaw, pitch=pitch)),
    )


def numbered_input(number, seq=None, pose=None, **id):
    frame = numbered_frame(number, **(pose or {}))
    return Input(ENCODED_FRAME_TYPE, frame.SerializeToString(), Id(number if seq is None else seq, **id))


def plan_kml(*views, count=0):
    """A next-flight KML as the SwiftMap server writes it, a pin for each
    (position, target), the area around them, and how many frames to send
    back for them."""
    pins = "".join(
        f"<Placemark><name>v{i}</name><LookAt><longitude>{t[1]}</longitude>"
        f"<latitude>{t[0]}</latitude><altitude>{t[2]}</altitude>"
        f"<altitudeMode>absolute</altitudeMode></LookAt>"
        f"<Point><altitudeMode>absolute</altitudeMode><coordinates>{p[1]},{p[0]},{p[2]}"
        f"</coordinates></Point></Placemark>"
        for i, (p, t) in enumerate(views))
    area = ("<Placemark><name>nfn_flight_area</name><Polygon><outerBoundaryIs><LinearRing>"
            "<coordinates>-79,40,0 -79.1,40,0 -79,40.1,0 -79,40,0</coordinates>"
            "</LinearRing></outerBoundaryIs></Polygon></Placemark>")
    data = f'<ExtendedData><Data name="reachback"><value>{count}</value></Data></ExtendedData>'
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>{data}{area}{pins}'
            '</Document></kml>').encode()


def numbers_received(server):
    """The numbers of the numbered frames the server got, in order."""
    return [int(jpeg.decode().removeprefix("jpeg-")) for jpeg, _ in server.received]
