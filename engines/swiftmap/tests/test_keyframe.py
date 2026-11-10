"""Tests of the keyframe engine (early discard): the tracker ported from the
SwiftMap server, the per-producer filter with its distance check, and the
engine end to end through a real Uriel server."""

import queue
import threading
import uuid
from dataclasses import dataclass

import cv2
import numpy as np
import pytest
import uriel

from steeleagle_protocol.v1.common import common_pb2
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.telemetry import telemetry_pb2
from swiftmap.early_discard import (
    NO_FIX, NOT_KEYFRAME, TOO_CLOSE, KeyframeFilter, haversine_m, run, to_gray,
)
from swiftmap.engine import ENCODED_FRAME_TYPE, RAW_FRAME_TYPE, FrameError
from swiftmap.keyframe import FrameTracker
from swiftmap.main import KEYFRAME_PLUGIN, ConfigError, parse_args

WIDTH, HEIGHT = 320, 240
# A blurred-noise scene, larger than a frame; each frame is a crop of it, so
# moving the crop is moving the camera.
_rng = np.random.default_rng(0)
SCENE = cv2.normalize(
    cv2.GaussianBlur((_rng.random((400, 640)) * 255).astype(np.uint8), (0, 0), 2),
    None, 0, 255, cv2.NORM_MINMAX,
)
FLAT = np.full((HEIGHT, WIDTH), 128, dtype=np.uint8)


def view(x, y=0):
    """The grayscale frame of the camera moved x px right and y px down."""
    return np.ascontiguousarray(SCENE[y:y + HEIGHT, x:x + WIDTH])


def keyframes(tracker, frames):
    return [tracker.is_keyframe(f) for f in frames]


def test_a_frame_is_a_keyframe_once_it_moved_min_disparity_from_the_last_one():
    # Disparity is about half a sideways move (see the next test), so 6 px is
    # ~3 and 30 px ~12-15
    tracker = FrameTracker(min_disparity=10)
    assert keyframes(tracker, [view(0), view(6), view(30), view(36), view(60)]) == [
        True, False, True, False, True,
    ]


def test_disparity_is_the_servers_mean_of_dx_and_dy():
    tracker = FrameTracker(min_disparity=10)
    tracker.is_keyframe(view(0))
    tracker.is_keyframe(view(6))
    # 6 px sideways: |dx| = 6 and |dy| = 0, so 3, where Euclidean would be 6
    assert tracker.last_disparity == pytest.approx(3.0, abs=0.5)


def test_a_featureless_frame_is_dropped_and_the_next_frame_starts_over():
    tracker = FrameTracker(min_disparity=10)
    assert keyframes(tracker, [FLAT, FLAT, view(0), view(2)]) == [False, False, True, False]


def test_without_min_disparity_every_frame_is_kept():
    assert keyframes(FrameTracker(min_disparity=0), [view(0), view(0), FLAT]) == [True] * 3


def test_a_new_resolution_starts_over():
    tracker = FrameTracker(min_disparity=10)
    tracker.is_keyframe(view(0))
    assert tracker.is_keyframe(cv2.resize(view(0), (WIDTH // 2, HEIGHT // 2)))


@dataclass
class Id:
    seq: int
    producer: str = "drone.rgb"
    session: int = 1


@dataclass
class Input:
    payload_type: str
    data: bytes
    id: Id


def raw_frame(gray, id=0, gps=None):
    rgb = cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)
    frame = frame_pb2.RawFrame(
        data=rgb.tobytes(), h_res=rgb.shape[1], v_res=rgb.shape[0], channels=3, id=id,
    )
    if gps is not None:
        frame.position_info.CopyFrom(telemetry_pb2.PositionInfo(
            global_position=common_pb2.GlobalPosition(latitude=gps[0], longitude=gps[1], altitude=10.0)
        ))
    return frame.SerializeToString()


def raw_input(gray, seq=0, gps=None, **id):
    return Input(RAW_FRAME_TYPE, raw_frame(gray, seq, gps), Id(seq, **id))


# Points ~1.1 m, ~11.1 m and ~12.2 m north of HOME
HOME, NEAR, FAR, FARTHER = (40.0, -79.0), (40.00001, -79.0), (40.0001, -79.0), (40.00011, -79.0)


def test_haversine():
    assert haversine_m(HOME, HOME) == 0
    assert haversine_m(HOME, FAR) == pytest.approx(11.12, abs=0.01)


def test_frames_within_send_distance_of_the_last_one_forwarded_are_dropped():
    f = KeyframeFilter(min_disparity=0, send_distance=5.0)
    kept = [f.keep(raw_input(view(0), seq, gps)) for seq, gps in enumerate([HOME, NEAR, FAR], 1)]
    assert kept == [True, False, True]
    assert f.dropped == {TOO_CLOSE: 1}


def test_frames_without_a_fix_are_dropped_unless_send_distance_is_0():
    f = KeyframeFilter(min_disparity=0, send_distance=5.0)
    assert not f.keep(raw_input(view(0), 1))
    assert f.dropped == {NO_FIX: 1}
    assert KeyframeFilter(min_disparity=0, send_distance=0).keep(raw_input(view(0), 1))


def test_a_frame_too_close_is_dropped_before_the_tracker_sees_it():
    f = KeyframeFilter(min_disparity=10, send_distance=5.0)
    assert f.keep(raw_input(view(0), 1, HOME))
    # Would be a keyframe, but is too close: dropped, so it is not the keyframe
    assert not f.keep(raw_input(view(30), 2, NEAR))
    # Compared with view(0), the last frame forwarded, not view(30) 6 px away
    assert f.keep(raw_input(view(36), 3, FAR))
    assert f.dropped == {TOO_CLOSE: 1}


def test_a_frame_far_enough_but_not_a_keyframe_is_dropped_and_the_distance_still_counts_from_the_last_one_forwarded():
    f = KeyframeFilter(min_disparity=10, send_distance=5.0)
    assert f.keep(raw_input(view(0), 1, HOME))
    assert not f.keep(raw_input(view(2), 2, FAR))
    # ~1.1 m from FAR, but ~12.2 m from HOME, the last frame forwarded
    assert f.keep(raw_input(view(36), 3, FARTHER))
    assert f.dropped == {NOT_KEYFRAME: 1}


def test_each_producer_and_session_is_tracked_on_its_own():
    f = KeyframeFilter(min_disparity=10, send_distance=5.0)
    assert f.keep(raw_input(view(0), 1, HOME, producer="a.rgb"))
    assert not f.keep(raw_input(view(2), 2, FAR, producer="a.rgb"))
    # Another camera's first frame is its first keyframe, wherever it is
    assert f.keep(raw_input(view(2), 1, HOME, producer="b.rgb"))
    # So is a's first frame of a new session
    assert f.keep(raw_input(view(2), 1, HOME, producer="a.rgb", session=2))
    assert (f.received, f.kept) == (4, 3)
    assert f.dropped == {NOT_KEYFRAME: 1}


def test_raw_and_jpeg_frames_are_read_in_grayscale():
    gray = view(0)
    assert np.array_equal(to_gray(frame_pb2.RawFrame.FromString(raw_frame(gray))), gray)
    jpeg = cv2.imencode(".jpg", gray, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()
    decoded = to_gray(frame_pb2.EncodedFrame(encoded_data=jpeg, codec=frame_pb2.CODEC_JPEG))
    assert decoded.shape == gray.shape
    assert np.abs(decoded.astype(int) - gray).mean() < 3


def test_h264_frames_and_other_payloads_are_not_read():
    f = KeyframeFilter(min_disparity=10, send_distance=0)
    h264 = frame_pb2.EncodedFrame(encoded_data=b"\0\0\1", codec=frame_pb2.CODEC_H264)
    with pytest.raises(FrameError):
        f.keep(Input(ENCODED_FRAME_TYPE, h264.SerializeToString(), Id(1)))
    with pytest.raises(FrameError):
        f.keep(Input("steeleagle_protocol/v1/messages/telemetry/Telemetry", b"", Id(2)))


def test_payload_types_may_be_written_with_dots():
    f = KeyframeFilter(min_disparity=10, send_distance=0)
    assert f.keep(Input(RAW_FRAME_TYPE.replace("/", "."), raw_frame(view(0)), Id(1)))


def test_keyframe_args_are_defaulted_and_checked():
    args = parse_args(KEYFRAME_PLUGIN, "")
    assert (args["min_disparity"], args["send_distance"]) == (40.0, 5.0)
    args = parse_args(KEYFRAME_PLUGIN, '{"min_disparity": 0, "send_distance": 0}')
    assert (args["min_disparity"], args["send_distance"]) == (0.0, 0.0)
    with pytest.raises(ConfigError):
        parse_args(KEYFRAME_PLUGIN, '{"min_disparity": -1}')
    with pytest.raises(ConfigError):
        parse_args(KEYFRAME_PLUGIN, '{"send_distance": -1}')


MAX_BYTES = 1 << 20
TIMEOUT_S = 10.0
FLOW_SPEC = """
[[producers]]
name = "{producer}"
tokens = 2

[[engines]]
name = "{keyframe}"
location = "local"

[[engines]]
name = "{sink}"
location = "local"

[[edges]]
from = "{producer}"
to = "{keyframe}"
timeout = "5s"

[[edges]]
from = "{keyframe}"
to = "{sink}"
timeout = "5s"
"""


def serve_sink(name, stop, ready, received):
    with uriel.Engine(name, max_output_size=MAX_BYTES) as engine:
        ready.set()
        while not stop.is_set():
            input = engine.next(0.05)
            if input is not None:
                received.put(frame_pb2.RawFrame.FromString(bytes(input.data)).id)
                engine.drop()


def outcome_of(subscription, seq):
    """What the engine subscribed to did with the input `seq`."""
    while (event := subscription.next(TIMEOUT_S)) is not None:
        if event.kind == uriel.EventKind.RESULT and event.id.seq == seq:
            return event.outcome
    raise AssertionError(f"no result for input {seq}")


def test_only_keyframes_reach_the_next_engine():
    suffix = uuid.uuid4().hex[:8]
    producer_name, keyframe_name, sink_name = f"camera-{suffix}", f"keyframe-{suffix}", f"sink-{suffix}"
    stop = threading.Event()
    received = queue.Queue()
    sink_ready, keyframe_ready = threading.Event(), threading.Event()
    threads = [
        threading.Thread(target=serve_sink, args=(sink_name, stop, sink_ready, received), daemon=True),
        threading.Thread(
            target=run, args=(keyframe_name, KeyframeFilter(min_disparity=10, send_distance=0)),
            kwargs={"stop": stop, "on_ready": keyframe_ready.set}, daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    assert sink_ready.wait(TIMEOUT_S) and keyframe_ready.wait(TIMEOUT_S)

    with (
        uriel.Server(f"server-{suffix}") as server,
        uriel.Client(f"client-{suffix}", f"server-{suffix}") as client,
        uriel.Producer(producer_name, input_type=RAW_FRAME_TYPE, max_input_size=MAX_BYTES) as producer,
        uriel.Subscription.engine(keyframe_name) as events,
    ):
        server.add_engine(keyframe_name)
        server.add_engine(sink_name)
        client.add_producer(producer_name)
        client.add_flow(
            f"flow-{suffix}",
            FLOW_SPEC.format(producer=producer_name, keyframe=keyframe_name, sink=sink_name),
        )
        try:
            # One at a time, so none is skipped for a newer one
            outcomes = [
                outcome_of(events, producer.send(raw_frame(view(x), id)))
                for id, x in enumerate([0, 4, 30])
            ]
            assert outcomes == [uriel.Outcome.FORWARD, uriel.Outcome.DROP, uriel.Outcome.FORWARD]
            assert [received.get(timeout=TIMEOUT_S) for _ in range(2)] == [0, 2]
            assert received.empty()
        finally:
            stop.set()
            for thread in threads:
                thread.join(TIMEOUT_S)
