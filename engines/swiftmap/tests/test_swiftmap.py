"""Tests of the swiftmap engine against a stand-in SwiftMap server."""

import json
import math
import socket

import cv2
import numpy as np
import pytest
from conftest import Id, Input, numbered_frame, numbered_input, numbers_received, plan_kml, position

from steeleagle_protocol.v1.messages.frame import frame_pb2
from swiftmap.client import FRAME, POLL, REACHBACK, REACHBACK_DONE, SwiftMapClient
from swiftmap.engine import (
    ENCODED_FRAME_TYPE, NFN_HOLD, NFN_RELEASE, POLL_S, RAW_FRAME_TYPE, REACHBACK_DONE_TYPE,
    REACHBACK_FRAME_TYPE, STATUS_S, FrameError, Request, Sender, SwiftMap, decode_status,
    encode_status, read_plan, stamp, unstamp,
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


def swiftmap(server):
    """The engine, sending each frame as it gets it, so the server has it
    when on_input returns."""
    return SwiftMap(Sender(SwiftMapClient("127.0.0.1", server.port)), jpeg_quality=90)


def test_raw_frames_go_to_the_server_as_jpegs_with_their_gps(server):
    swiftmap(server).on_input(raw_input(1, (40.0, -79.0, 12.0)))
    [(jpeg, gps)] = server.received
    assert gps == (40.0, -79.0, 12.0) and server.kinds == [FRAME]
    assert cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR).shape == (24, 32, 3)


def test_jpeg_frames_go_to_the_server_as_they_are(server):
    input, jpeg = jpeg_input(1, (40.0, -79.0))
    swiftmap(server).on_input(input)
    assert server.received[0][0] == jpeg


def test_h264_frames_are_not_sent(server):
    input, _ = jpeg_input(1, (40.0, -79.0), codec=frame_pb2.CODEC_H264)
    swiftmap(server).on_input(input)
    assert server.received == []


def test_what_the_server_sends_back_is_kept_in_order(server):
    server.replies.extend([NFN_HOLD, KML])
    engine = swiftmap(server)
    for n in range(3):
        engine.on_input(raw_input(n, (40.0, -79.0)))
    assert engine.sender.take_replies() == [NFN_HOLD, KML]
    assert engine.sender.take_replies() == []


def test_a_planned_area_is_the_next_result(server):
    engine = swiftmap(server)
    engine.on_reply(KML)
    assert engine.take_result() == KML
    assert engine.take_result() == b""


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


def sent_back(request_id, number, frame_number):
    frame = numbered_frame(frame_number).SerializeToString()
    return Input(REACHBACK_FRAME_TYPE, stamp(request_id, number, frame), Id(9))


def end_of(request_id, count):
    return Input(REACHBACK_DONE_TYPE, stamp(request_id, count), Id(9))


def test_frames_sent_back_and_their_end_go_to_the_server_marked(server):
    engine = swiftmap(server)
    engine.on_input(numbered_input(1))
    engine.on_reply(plan_kml(VIEW, count=2))
    request_id = engine.request.id
    engine.on_input(sent_back(request_id, 0, 7))
    engine.on_input(sent_back(request_id, 1, 3))
    engine.on_input(end_of(request_id, 2))
    assert numbers_received(server) == [1, 7, 3]
    assert server.kinds == [FRAME, REACHBACK, REACHBACK, REACHBACK_DONE]


def test_what_is_sent_back_twice_late_or_for_an_old_request_is_dropped(server):
    engine = swiftmap(server)
    engine.on_reply(plan_kml(VIEW, count=2))
    old = engine.request.id
    engine.on_reply(plan_kml(VIEW, count=2))
    current = engine.request.id
    for input in (sent_back(old, 0, 5), sent_back(current, 0, 7), sent_back(current, 0, 7),
                  end_of(old, 1), end_of(current, 2), end_of(current, 2),
                  sent_back(current, 1, 8)):
        engine.on_input(input)
    assert numbers_received(server) == [7]
    assert server.kinds == [REACHBACK, REACHBACK_DONE]


def test_a_payload_too_short_to_be_stamped_is_not_sent(server):
    engine = swiftmap(server)
    engine.on_input(Input(REACHBACK_FRAME_TYPE, b"short", Id(9)))
    engine.on_input(Input(REACHBACK_DONE_TYPE, b"", Id(9)))
    assert server.kinds == []
    with pytest.raises(FrameError):
        unstamp(b"short")
    assert unstamp(stamp(7, 2, b"jpeg")) == (7, 2, b"jpeg")


def test_a_plan_has_targets_and_a_count():
    assert read_plan(plan_kml(VIEW, count=4)) == ([list(VIEW[1])], 4)
    assert read_plan(KML) == ([], 0)
    with pytest.raises(ValueError):
        read_plan(b"not xml")


def test_a_plan_with_targets_and_a_count_is_the_request(server):
    engine = swiftmap(server)
    engine.on_input(numbered_input(4))
    engine.on_reply(plan_kml(VIEW, VIEW, count=3))
    hold, request = decode_status(engine.take_status(0.0))
    assert not hold and (request.targets, request.count) == ([list(VIEW[1])] * 2, 3)
    engine.on_reply(plan_kml(VIEW))
    assert decode_status(engine.take_status(0.0)) == (False, None)
    engine.on_reply(plan_kml(VIEW, count=1))
    engine.on_reply(b"not xml")
    assert engine.request is None


def test_request_ids_are_new_after_a_restart(server):
    engine = swiftmap(server)
    engine.on_reply(plan_kml(VIEW, count=1))
    restarted = swiftmap(server)
    restarted.on_reply(plan_kml(VIEW, count=1))
    assert restarted.request.id > engine.request.id


def test_the_status_is_told_at_once_on_a_change_and_every_second_once_nfn_starts(server):
    engine = swiftmap(server)
    engine.on_input(numbered_input(4))
    assert engine.take_status(0.0) is None
    engine.on_reply(NFN_HOLD)
    assert decode_status(engine.take_status(0.0)) == (True, None)
    assert engine.take_status(STATUS_S / 2) is None
    assert decode_status(engine.take_status(STATUS_S)) == (True, None)
    engine.on_reply(NFN_RELEASE)
    assert decode_status(engine.take_status(STATUS_S)) == (False, None)
    assert engine.take_status(STATUS_S * 1.5) is None
    assert decode_status(engine.take_status(STATUS_S * 2)) == (False, None)


def test_a_hold_and_release_are_kept_and_it_polls_while_held(server):
    engine = swiftmap(server)
    engine.on_input(numbered_input(4))
    engine.on_reply(NFN_HOLD)
    assert engine.holding and engine.take_result() == b""
    engine.poll(engine.sender.last_sent + POLL_S / 2)
    engine.poll(engine.sender.last_sent + POLL_S)
    engine.on_reply(NFN_RELEASE)
    engine.poll(engine.sender.last_sent + POLL_S)
    assert not engine.holding
    assert server.kinds == [FRAME, POLL]


def test_a_hold_or_release_withdraws_the_request(server):
    engine = swiftmap(server)
    engine.on_reply(NFN_HOLD)
    engine.on_reply(plan_kml(VIEW, count=1))
    assert engine.request is not None
    engine.on_reply(NFN_RELEASE)
    assert engine.request is None


def test_a_status_says_whether_to_hold_and_the_request():
    request = Request(7, [list(VIEW[1])], 3)
    assert json.loads(encode_status(True, request)) == {
        "hold": True, "request": {"id": 7, "targets": [list(VIEW[1])], "count": 3}}
    assert decode_status(encode_status(True, request)) == (True, request)
    assert decode_status(encode_status(False, None)) == (False, None)
    for bad in (b"[]", b"{}", b'{"hold": 1, "request": null}',
                b'{"hold": true, "request": {"id": 1, "targets": [[1, 2]], "count": 1}}',
                b'{"hold": true, "request": {"id": 1, "targets": [], "count": -1}}',
                b'{"hold": true, "request": {"id": -1, "targets": [], "count": 1}}',
                b"\xff"):
        with pytest.raises(ValueError):
            decode_status(bad)


def test_args_are_defaulted_and_checked():
    args = parse_args(SWIFTMAP_PLUGIN, '{"server": "mapper"}')
    assert (args["server"], args["server_port"], args["jpeg_quality"]) == ("mapper", 43322, 95)
    with pytest.raises(ConfigError):
        parse_args(SWIFTMAP_PLUGIN, '{"server": ""}')
    with pytest.raises(ConfigError):
        parse_args(SWIFTMAP_PLUGIN, '{"jpeg_quality": 0}')
