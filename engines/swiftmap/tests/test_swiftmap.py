"""Tests of the swiftmap engine against a stand-in SwiftMap server."""

import json
import math
import socket

import cv2
import numpy as np
import pytest
from conftest import Id, Input, numbered_frame, numbered_input, numbers_received, position

from steeleagle_protocol.v1.messages.frame import frame_pb2
from swiftmap.client import SwiftMapClient
from swiftmap.engine import (
    ENCODED_FRAME_TYPE, FILE_SCAN_INTERVAL_S, RAW_FRAME_TYPE, Sender, SwiftMap, decode_request,
    encode_request, frame_filename,
)
from swiftmap.inorder import RETRY_INTERVAL_S
from swiftmap.main import SWIFTMAP_PLUGIN, ConfigError, parse_args

KML = b"<kml>next flight</kml>"
GAP_TIMEOUT = 30.0


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


def swiftmap(server, gap_timeout=0, files_dir=None):
    """The engine, sending each frame as it gets it, so the server has it
    when on_input returns."""
    sender = Sender(SwiftMapClient("127.0.0.1", server.port))
    return SwiftMap(sender, jpeg_quality=90, gap_timeout=gap_timeout, files_dir=files_dir)


def test_raw_frames_go_to_the_server_as_jpegs_with_their_gps(server):
    swiftmap(server).on_input(raw_input(1, (40.0, -79.0, 12.0)), 0.0)
    [(jpeg, gps)] = server.received
    assert gps == (40.0, -79.0, 12.0)
    assert cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR).shape == (24, 32, 3)


def test_jpeg_frames_go_to_the_server_as_they_are(server):
    input, jpeg = jpeg_input(1, (40.0, -79.0))
    swiftmap(server).on_input(input, 0.0)
    assert server.received[0][0] == jpeg


def test_h264_frames_are_not_sent(server):
    input, _ = jpeg_input(1, (40.0, -79.0), codec=frame_pb2.CODEC_H264)
    swiftmap(server).on_input(input, 0.0)
    assert server.received == []


def test_a_planned_area_is_the_next_result(server):
    server.replies.append(KML)
    engine = swiftmap(server)
    engine.on_input(raw_input(1, (40.0, -79.0)), 0.0)
    assert engine.sender.take_kml() == KML
    assert engine.sender.take_kml() == b""


def test_a_frame_without_a_fix_is_sent_with_a_nan_one(server):
    swiftmap(server).on_input(raw_input(1), 0.0)
    [(_, gps)] = server.received
    assert all(math.isnan(v) for v in gps)


def test_the_engine_reconnects_when_the_server_drops_the_connection(server):
    engine = swiftmap(server)
    engine.on_input(raw_input(1, (40.0, -79.0)), 0.0)
    engine.sender.client.sock.shutdown(socket.SHUT_RDWR)
    engine.on_input(raw_input(2, (41.0, -79.0)), 0.0)
    assert len(server.received) == 2


def test_an_unreachable_server_maps_nothing():
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    sender = Sender(SwiftMapClient("127.0.0.1", port))
    SwiftMap(sender, jpeg_quality=90, gap_timeout=0).on_input(raw_input(1, (40.0, -79.0)), 0.0)
    assert sender.sent == 0


def test_a_started_sender_sends_on_its_own_thread(server):
    with Sender(SwiftMapClient("127.0.0.1", server.port)) as sender:
        engine = SwiftMap(sender, jpeg_quality=90, gap_timeout=GAP_TIMEOUT)
        for n in range(3):
            engine.on_input(numbered_input(n), 0.0)
    # Leaving it waits for the frames queued
    assert numbers_received(server) == [0, 1, 2]


def test_without_gap_timeout_frames_are_sent_as_they_come(server):
    engine = swiftmap(server)
    for n in (1, 2, 5, 3):
        engine.on_input(numbered_input(n), 0.0)
    assert numbers_received(server) == [1, 2, 5, 3]
    assert engine.poll(0.0) == []


def send_back(files_dir, *numbers):
    """Puts the frames in `files_dir` as the reachback engine sends them."""
    for n in numbers:
        (files_dir / frame_filename(n)).write_bytes(numbered_frame(n).SerializeToString())


def test_frames_after_lost_ones_are_held_until_they_are_sent_back(server, tmp_path):
    engine = swiftmap(server, GAP_TIMEOUT, tmp_path)
    for n in (1, 2, 3, 6, 7, 8):
        engine.on_input(numbered_input(n), 0.0)
    assert numbers_received(server) == [1, 2, 3]
    # It asks for the frames between 3 and 6, about the last input
    [(id, payload)] = engine.poll(0.0)
    assert decode_request(payload) == (4, 5)
    assert (id.producer, id.session, id.seq) == ("camera", 1, 8)

    send_back(tmp_path, 5)
    engine.poll(1.0)
    assert numbers_received(server) == [1, 2, 3]
    send_back(tmp_path, 4)
    engine.poll(2.0)
    assert numbers_received(server) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert list(tmp_path.iterdir()) == []
    engine.on_input(numbered_input(9), 2.0)
    assert numbers_received(server)[-1] == 9


def test_requests_repeat_while_frames_are_missing(server, tmp_path):
    engine = swiftmap(server, GAP_TIMEOUT, tmp_path)
    for n in (1, 3, 4, 6):
        engine.on_input(numbered_input(n), 0.0)
    assert [decode_request(p) for _, p in engine.poll(0.0)] == [(2, 2), (5, 5)]
    assert engine.poll(RETRY_INTERVAL_S / 2) == []
    send_back(tmp_path, 2)
    # Only the frame still missing is asked for again
    assert [decode_request(p) for _, p in engine.poll(RETRY_INTERVAL_S)] == [(5, 5)]
    assert numbers_received(server) == [1, 2, 3, 4]


def test_frames_still_missing_after_gap_timeout_are_given_up_on(server, tmp_path):
    engine = swiftmap(server, GAP_TIMEOUT, tmp_path)
    for n in (1, 4, 5):
        engine.on_input(numbered_input(n), 0.0)
    engine.poll(GAP_TIMEOUT - 1)
    assert numbers_received(server) == [1]
    engine.poll(GAP_TIMEOUT)
    assert numbers_received(server) == [1, 4, 5]
    # Sent back too late: its file is removed, and the frame not sent
    send_back(tmp_path, 2)
    engine.poll(GAP_TIMEOUT + FILE_SCAN_INTERVAL_S)
    assert numbers_received(server) == [1, 4, 5]
    assert list(tmp_path.iterdir()) == []


def test_other_files_and_bad_frames_are_left_alone_or_ignored(server, tmp_path):
    engine = swiftmap(server, GAP_TIMEOUT, tmp_path)
    for n in (1, 3):
        engine.on_input(numbered_input(n), 0.0)
    (tmp_path / "mission.zip").write_bytes(b"zip")
    # A file named for frame 2 that holds frame 7
    (tmp_path / frame_filename(2)).write_bytes(numbered_frame(7).SerializeToString())
    engine.poll(0.0)
    assert numbers_received(server) == [1]
    assert [p.name for p in tmp_path.iterdir()] == ["mission.zip"]


def test_a_new_session_sends_the_frames_held_and_starts_over(server, tmp_path):
    engine = swiftmap(server, GAP_TIMEOUT, tmp_path)
    for n in (1, 3):
        engine.on_input(numbered_input(n), 0.0)
    engine.on_input(numbered_input(0, session=2), 0.0)
    engine.on_input(numbered_input(1, session=2), 0.0)
    assert numbers_received(server) == [1, 3, 0, 1]


def test_numbers_starting_over_send_the_frames_held(server, tmp_path):
    # The JPEG encoder restarted
    engine = swiftmap(server, GAP_TIMEOUT, tmp_path)
    for n in (5, 7, 0, 1):
        engine.on_input(numbered_input(n), 0.0)
    assert numbers_received(server) == [5, 7, 0, 1]


def test_requests_name_a_range_of_frames():
    assert json.loads(encode_request(4, 5)) == {"first": 4, "last": 5}
    assert decode_request(encode_request(4, 5)) == (4, 5)
    for bad in (b"[]", b"{}", b'{"first": 5, "last": 4}', b'{"first": "a", "last": 1}', b"\xff"):
        with pytest.raises(ValueError):
            decode_request(bad)


def test_args_are_defaulted_and_checked():
    args = parse_args(SWIFTMAP_PLUGIN, '{"server": "mapper"}')
    assert (args["server"], args["server_port"], args["jpeg_quality"], args["gap_timeout"]) == (
        "mapper", 43322, 95, 30.0
    )
    with pytest.raises(ConfigError):
        parse_args(SWIFTMAP_PLUGIN, '{"server": ""}')
    with pytest.raises(ConfigError):
        parse_args(SWIFTMAP_PLUGIN, '{"jpeg_quality": 0}')
    with pytest.raises(ConfigError):
        parse_args(SWIFTMAP_PLUGIN, '{"gap_timeout": -1}')
