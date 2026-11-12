"""Tests of the swiftmap engine against a stand-in SwiftMap server."""

import json
import math
import socket

import cv2
import numpy as np
import pytest
from conftest import Id, Input, numbered_frame, numbered_input, numbers_received, plan_kml, position

from steeleagle_protocol.v1.messages.frame import frame_pb2
from swiftmap.client import SwiftMapClient
from swiftmap.engine import (
    ENCODED_FRAME_TYPE, RAW_FRAME_TYPE, Sender, SwiftMap, decode_request, encode_request,
    frame_filename, viewpoints,
)
from swiftmap.main import SWIFTMAP_PLUGIN, ConfigError, parse_args

KML = b"<kml>next flight</kml>"
VIEW = ((40.0, -79.0, 15.0), (40.0001, -79.0, 0.0))


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


def swiftmap(server, files_dir=None):
    """The engine, sending each frame as it gets it, so the server has it
    when on_input returns."""
    return SwiftMap(Sender(SwiftMapClient("127.0.0.1", server.port)), jpeg_quality=90,
                    files_dir=files_dir)


def test_raw_frames_go_to_the_server_as_jpegs_with_their_gps(server):
    swiftmap(server).on_input(raw_input(1, (40.0, -79.0, 12.0)))
    [(jpeg, gps)] = server.received
    assert gps == (40.0, -79.0, 12.0)
    assert cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR).shape == (24, 32, 3)


def test_jpeg_frames_go_to_the_server_as_they_are(server):
    input, jpeg = jpeg_input(1, (40.0, -79.0))
    swiftmap(server).on_input(input)
    assert server.received[0][0] == jpeg


def test_h264_frames_are_not_sent(server):
    input, _ = jpeg_input(1, (40.0, -79.0), codec=frame_pb2.CODEC_H264)
    swiftmap(server).on_input(input)
    assert server.received == []


def test_a_planned_area_is_the_next_result(server):
    server.replies.append(KML)
    engine = swiftmap(server)
    engine.on_input(raw_input(1, (40.0, -79.0)))
    assert engine.sender.take_kml() == KML
    assert engine.sender.take_kml() == b""


def test_a_frame_without_a_fix_is_sent_with_a_nan_one(server):
    swiftmap(server).on_input(raw_input(1))
    [(_, gps)] = server.received
    assert all(math.isnan(v) for v in gps)


def test_the_engine_reconnects_when_the_server_drops_the_connection(server):
    engine = swiftmap(server)
    engine.on_input(raw_input(1, (40.0, -79.0)))
    engine.sender.client.sock.shutdown(socket.SHUT_RDWR)
    engine.on_input(raw_input(2, (41.0, -79.0)))
    assert len(server.received) == 2


def test_an_unreachable_server_maps_nothing():
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    sender = Sender(SwiftMapClient("127.0.0.1", port))
    SwiftMap(sender, jpeg_quality=90).on_input(raw_input(1, (40.0, -79.0)))
    assert sender.sent == 0


def test_a_started_sender_sends_on_its_own_thread(server):
    with Sender(SwiftMapClient("127.0.0.1", server.port)) as sender:
        engine = SwiftMap(sender, jpeg_quality=90)
        for n in (1, 2, 5, 3):
            engine.on_input(numbered_input(n))
    assert numbers_received(server) == [1, 2, 5, 3]


def test_viewpoints_are_read_from_the_pins_of_a_plan():
    [(lat, lon, alt, tlat, tlon, talt)] = viewpoints(plan_kml(VIEW))
    assert (lat, lon, alt, tlat, tlon, talt) == (*VIEW[0], *VIEW[1])
    assert viewpoints(KML) == []
    with pytest.raises(ValueError):
        viewpoints(b"not xml")


def test_a_plan_asks_for_the_frames_closest_to_its_viewpoints(server):
    engine = swiftmap(server)
    for n in (1, 4):
        engine.on_input(numbered_input(n))
    engine.on_kml(Id(4), plan_kml(VIEW, VIEW))
    engine.on_kml(Id(4), b"<kml>no viewpoints</kml>")
    engine.on_kml(Id(4), b"not xml")
    [(id, payload)] = engine.take_requests()
    assert id.seq == 4
    assert decode_request(payload) == ([[*VIEW[0], *VIEW[1]]] * 2, {1, 4})
    assert engine.take_requests() == []


def test_a_new_session_forgets_the_frames_sent(server):
    engine = swiftmap(server)
    engine.on_input(numbered_input(1))
    engine.on_input(numbered_input(2, session=2))
    engine.on_kml(Id(2), plan_kml(VIEW))
    [(_, payload)] = engine.take_requests()
    assert decode_request(payload)[1] == {2}


def send_back(files_dir, *numbers):
    """Puts the frames in `files_dir` as the reachback engine sends them."""
    for n in numbers:
        (files_dir / frame_filename(n)).write_bytes(numbered_frame(n).SerializeToString())


def test_frames_sent_back_are_sent_in_order_once(server, tmp_path):
    engine = swiftmap(server, tmp_path)
    engine.on_input(numbered_input(1))
    send_back(tmp_path, 7, 3, 1)
    engine.poll(0.0)
    send_back(tmp_path, 3)
    engine.poll(0.0)
    engine.poll(1.0)
    assert numbers_received(server) == [1, 3, 7]
    assert list(tmp_path.iterdir()) == []


def test_other_files_are_left_alone_and_bad_frames_removed(server, tmp_path):
    engine = swiftmap(server, tmp_path)
    (tmp_path / "mission.zip").write_bytes(b"zip")
    (tmp_path / frame_filename(2)).write_bytes(numbered_frame(7).SerializeToString())
    (tmp_path / frame_filename(3)).write_bytes(b"\xff\xff")
    engine.poll(0.0)
    assert server.received == []
    assert [p.name for p in tmp_path.iterdir()] == ["mission.zip"]


def test_requests_name_viewpoints_and_frames_to_leave_out():
    views = [[*VIEW[0], *VIEW[1]]]
    assert json.loads(encode_request(views, {3, 1})) == {"viewpoints": views, "exclude": [1, 3]}
    assert decode_request(encode_request(views, {3})) == (views, {3})
    for bad in (b"[]", b"{}", b'{"viewpoints": [[1, 2]], "exclude": []}',
                b'{"viewpoints": [], "exclude": ["a"]}', b"\xff"):
        with pytest.raises(ValueError):
            decode_request(bad)


def test_args_are_defaulted_and_checked():
    args = parse_args(SWIFTMAP_PLUGIN, '{"server": "mapper"}')
    assert (args["server"], args["server_port"], args["jpeg_quality"]) == ("mapper", 43322, 95)
    with pytest.raises(ConfigError):
        parse_args(SWIFTMAP_PLUGIN, '{"server": ""}')
    with pytest.raises(ConfigError):
        parse_args(SWIFTMAP_PLUGIN, '{"jpeg_quality": 0}')
