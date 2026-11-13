"""Tests of the reachback engine: its store, the frames it picks for the weak
targets of a plan, keyframe selection pausing while they are sent back, and
frames sent back through a real Uriel flow to the swiftmap engine."""

import math
import threading
import time
import uuid

import pytest
import uriel
from conftest import TARGET, Id, Input, near, numbered_frame, numbered_input, numbers_received, plan_kml

from steeleagle_protocol.v1.messages.frame import frame_pb2
from swiftmap import engine, reachback
from swiftmap.client import FRAME, REACHBACK, REACHBACK_DONE, SwiftMapClient
from swiftmap.engine import (
    ENCODED_FRAME_TYPE, REACHBACK_DONE_TYPE, REACHBACK_FRAME_TYPE, Sender, SwiftMap,
    encode_request,
)
from swiftmap.main import REACHBACK_PLUGIN, ConfigError, parse_args
from swiftmap.reachback import DROP, FORWARD, STORE_INTERVAL_S, FrameStore, Reachback

TIMEOUT_S = 10.0
POSE = (40.0, -79.0, 10.0, 0.0, -90.0)


def looking_at(east, north, up, turn=0.0, target=TARGET):
    """A frame's pose `east`, `north` and `up` metres from `target`, looking
    at it, turned `turn` degrees away."""
    return {"gps": near(east, north, up, target),
            "yaw": math.degrees(math.atan2(-east, -north)) + turn,
            "pitch": math.degrees(math.atan2(-up, math.hypot(east, north)))}


WEAK = list(TARGET)
BEST = looking_at(0, -14, 10)
SLIGHT = looking_at(8, -13, 10, turn=10)
OFF = looking_at(0, -14, 10, turn=40)
AWAY = looking_at(0, -14, 10, turn=180)
FAR = looking_at(0, -60, 10)


class Keyframes:
    """Keeps the frames numbered in `numbers`, as the keyframe filter would."""

    def __init__(self, numbers):
        self.numbers = set(numbers)

    def keep(self, input):
        return frame_pb2.EncodedFrame.FromString(bytes(input.data)).id in self.numbers


def number_of(payload):
    return frame_pb2.EncodedFrame.FromString(payload).id


@pytest.fixture
def store():
    s = FrameStore()
    yield s
    s.close()


def test_the_store_gives_the_unsent_frames_of_a_stream(store):
    store.add("camera", 1, 5, POSE, b"f5")
    store.add("camera", 1, 6, POSE, b"f6")
    store.add("camera", 1, 9, POSE, b"keyframe", sent=True)
    store.add("camera", 2, 7, POSE, b"another session")
    rowids, numbers, poses = store.unsent("camera", 1)
    assert list(numbers) == [5, 6] and poses.tolist() == [list(POSE)] * 2
    assert store.take(rowids[0]) == b"f5"
    assert list(store.unsent("camera", 1)[1]) == [6]
    assert len(store.unsent("camera", 3)[0]) == 0


def test_only_the_newest_frames_are_kept():
    store = FrameStore(keep=3)
    for number in range(5):
        store.add("camera", 1, number, POSE, b"")
    assert list(store.unsent("camera", 1)[1]) == [2, 3, 4]
    store.close()


def test_the_database_has_no_journal_file(tmp_path):
    store = FrameStore(str(tmp_path / "frames.sqlite3"))
    store.add("camera", 1, 0, POSE, b"f0")
    assert [p.name for p in tmp_path.iterdir()] == ["frames.sqlite3"]
    store.close()


def test_keyframes_are_forwarded_and_stored_as_sent_and_others_stored_every_interval(store):
    rb = Reachback(store, Keyframes({2}))
    assert rb.on_input(numbered_input(1), 0.0) == DROP
    assert rb.on_input(numbered_input(2), STORE_INTERVAL_S / 2) == FORWARD
    assert rb.on_input(numbered_input(3), STORE_INTERVAL_S / 2) == DROP
    assert rb.on_input(numbered_input(4), STORE_INTERVAL_S * 2) == DROP
    no_pose = numbered_frame(5)
    no_pose.ClearField("gimbal_info")
    rb.on_input(Input(ENCODED_FRAME_TYPE, no_pose.SerializeToString(), Id(5)), 9.0)
    assert list(store.unsent("camera", 1)[1]) == [1, 4]


def answering(store, keep=()):
    rb = Reachback(store, Keyframes(keep), interval=0)
    for n, pose in enumerate((AWAY, FAR, OFF, SLIGHT, BEST)):
        rb.on_input(numbered_input(n, pose=pose), 0.0)
    return rb


def test_frames_covering_the_targets_are_sent_back_before_any_more_keyframes(store):
    rb = answering(store, keep={20, 21})
    rb.on_request(Id(9), encode_request([WEAK, WEAK], 2))
    sent = [rb.on_input(numbered_input(n), 1.0) for n in (20, 21, 22)]
    assert [(t, number_of(p)) for t, p in sent[:2]] == [(REACHBACK_FRAME_TYPE, 4),
                                                         (REACHBACK_FRAME_TYPE, 3)]
    assert sent[2] == (REACHBACK_DONE_TYPE, b"done")
    assert rb.on_input(numbered_input(21), 1.0) == FORWARD
    assert 20 in store.unsent("camera", 1)[1]


def test_a_request_sends_at_most_its_count_and_the_end_even_with_none(store):
    rb = answering(store)
    rb.on_request(Id(9), encode_request([WEAK], 5))
    assert [t for t, _ in rb.queue] == [REACHBACK_FRAME_TYPE] * 2 + [REACHBACK_DONE_TYPE]
    rb.queue = []
    rb.on_request(Id(9), encode_request([WEAK], 5))
    assert rb.queue == [(REACHBACK_DONE_TYPE, b"done")]
    with pytest.raises(ValueError):
        rb.on_request(Id(9), b"{}")


def test_reachback_args_are_defaulted_and_checked():
    args = parse_args(REACHBACK_PLUGIN, "")
    assert (args["remote"], args["send_distance"], args["min_disparity"]) == ("swiftmap", 5.0, 40.0)
    for bad in ('{"remote": " "}', '{"send_distance": -1}', '{"min_disparity": -1}'):
        with pytest.raises(ConfigError):
            parse_args(REACHBACK_PLUGIN, bad)


MAX_BYTES = 1 << 16
FLOW_SPEC = """
[[producers]]
name = "{producer}"
tokens = 2

[[engines]]
name = "{reachback}"
location = "local"

[[engines]]
name = "{swiftmap}"
location = "local"

[[edges]]
from = "{producer}"
to = "{reachback}"
timeout = "5s"

[[edges]]
from = "{reachback}"
to = "{swiftmap}"
timeout = "5s"
"""


def serve_swiftmap(name, port, stop, ready):
    with Sender(SwiftMapClient("127.0.0.1", port)) as sender:
        swiftmap = SwiftMap(sender, jpeg_quality=90)
        engine.run(name, swiftmap, max_output_size=MAX_BYTES, stop=stop, on_ready=ready.set)


def serve_reachback(name, remote, rb, stop, ready):
    reachback.run(name, rb, remote, max_output_size=MAX_BYTES, stop=stop, on_ready=ready.set)


def outcome_of(subscription, seq):
    """What the engine subscribed to did with the input `seq`."""
    while (event := subscription.next(TIMEOUT_S)) is not None:
        if event.kind == uriel.EventKind.RESULT and event.id.seq == seq:
            return event.outcome
    raise AssertionError(f"no result for input {seq}")


def test_frames_covering_a_weak_target_come_back_through_the_flow(server):
    server.replies.append(plan_kml((near(0, -15, 10), TARGET), count=2))
    suffix = uuid.uuid4().hex[:8]
    producer_name, reachback_name, swiftmap_name = (
        f"camera-{suffix}", f"reachback-{suffix}", f"swiftmap-{suffix}"
    )
    store = FrameStore()
    rb = Reachback(store, Keyframes({0, 6}), interval=0)
    stop = threading.Event()
    readies = [threading.Event() for _ in range(2)]
    threads = [
        threading.Thread(target=serve_swiftmap, daemon=True,
                         args=(swiftmap_name, server.port, stop, readies[0])),
        threading.Thread(target=serve_reachback, daemon=True,
                         args=(reachback_name, swiftmap_name, rb, stop, readies[1])),
    ]
    poses = [AWAY, BEST, SLIGHT, OFF, AWAY, AWAY, AWAY, AWAY, AWAY, AWAY]
    try:
        for thread, ready in zip(threads, readies):
            thread.start()
            assert ready.wait(TIMEOUT_S)
        with (
            uriel.Server(f"server-{suffix}") as uriel_server,
            uriel.Client(f"client-{suffix}", f"server-{suffix}") as client,
            uriel.Producer(producer_name, input_type=ENCODED_FRAME_TYPE,
                           max_input_size=MAX_BYTES) as producer,
            uriel.Subscription.engine(reachback_name) as reachback_events,
            uriel.Subscription.engine(swiftmap_name) as swiftmap_events,
        ):
            for name in (reachback_name, swiftmap_name):
                uriel_server.add_engine(name)
            client.add_producer(producer_name)
            client.add_flow(f"flow-{suffix}", FLOW_SPEC.format(
                producer=producer_name, reachback=reachback_name, swiftmap=swiftmap_name,
            ))
            for n, pose in enumerate(poses):
                seq = producer.send(numbered_frame(n, **pose).SerializeToString())
                if outcome_of(reachback_events, seq) != uriel.Outcome.DROP:
                    outcome_of(swiftmap_events, seq)
                if n == 6:
                    deadline = time.monotonic() + TIMEOUT_S
                    while not rb.queue and time.monotonic() < deadline:
                        time.sleep(0.05)
            deadline = time.monotonic() + TIMEOUT_S
            while len(server.kinds) < 5 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert numbers_received(server) == [0, 6, 1, 2]
            assert server.kinds == [FRAME, FRAME, REACHBACK, REACHBACK, REACHBACK_DONE]
    finally:
        stop.set()
        for thread in threads:
            thread.join(TIMEOUT_S)
        store.close()
