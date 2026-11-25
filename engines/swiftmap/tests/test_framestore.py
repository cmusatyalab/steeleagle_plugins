"""Tests of the framestore engine: its store, keyframes published unless
holding, the frames it picks for the weak targets of a request, published one
at a time, and a real Uriel flow where its producers feed the swiftmap engine."""

import contextlib
import math
import threading
import time
import uuid
from dataclasses import dataclass

import pytest
import uriel
from conftest import TARGET, Id, Input, near, numbered_frame, numbered_input, numbers_received, plan_kml

from steeleagle_protocol.v1.messages.frame import frame_pb2
from swiftmap import framestore, remote
from swiftmap.framestore import END, STORE_INTERVAL_S, FrameStore, FrameStoreEngine
from swiftmap.main import FRAMESTORE_PLUGIN, ConfigError, parse_args
from swiftmap.protocol import ENCODED_FRAME_TYPE, KEYFRAMES, SENDBACK, encode_status, producer_name
from swiftmap.remote import FRAME, NFN_HOLD, POLL, REACHBACK, REACHBACK_DONE, Sender, SwiftMap, SwiftMapClient

TIMEOUT_S = 10.0
POSE = (40.0, -79.0, 10.0, 0.0, -90.0)


def looking_at(east, north, up, turn=0.0, target=TARGET):
    """A frame's pose `east`, `north` and `up` metres from `target`, looking
    at it, turned `turn` degrees away."""
    return {"gps": near(east, north, up, target),
            "yaw": math.degrees(math.atan2(-east, -north)) + turn,
            "pitch": math.degrees(math.atan2(-up, math.hypot(east, north)))}


WEAK = list(TARGET)
NOWHERE = [41.0, -80.0, 0.0]
BEST = looking_at(0, -14, 10)
SLIGHT = looking_at(8, -13, 10, turn=10)
OFF = looking_at(0, -14, 10, turn=40)
AWAY = looking_at(0, -14, 10, turn=180)
FAR = looking_at(0, -60, 10)


class Keyframes:
    """Keeps the frames numbered in `numbers`, as the keyframe filter would,
    and counts the frames it is asked about."""

    def __init__(self, numbers):
        self.numbers = set(numbers)
        self.asked = 0

    def keep(self, input):
        self.asked += 1
        return frame_pb2.EncodedFrame.FromString(bytes(input.data)).id in self.numbers


@dataclass
class Event:
    kind: uriel.EventKind
    id: Id = None


def number_of(payload):
    return frame_pb2.EncodedFrame.FromString(payload).id


def status(hold, request=None):
    return encode_status(hold, request)


@pytest.fixture
def store():
    s = FrameStore()
    yield s
    s.close()


def test_the_store_gives_the_unsent_frames_and_takes_them_once(store):
    store.add(5, POSE, b"f5")
    store.add(6, POSE, b"f6")
    store.add(9, POSE, b"keyframe", sent=True)
    rowids, numbers, poses = store.unsent()
    assert list(numbers) == [5, 6] and poses.tolist() == [list(POSE)] * 2
    assert store.take(rowids[0]) == b"f5"
    assert list(store.unsent()[1]) == [6]


def test_only_the_newest_frames_are_kept():
    store = FrameStore(keep=3)
    for number in range(5):
        store.add(number, POSE, b"")
    assert list(store.unsent()[1]) == [2, 3, 4]
    assert store.take(1) is None
    store.close()


def test_the_database_has_no_journal_file(tmp_path):
    store = FrameStore(str(tmp_path / "frames.sqlite3"))
    store.add(0, POSE, b"f0")
    assert [p.name for p in tmp_path.iterdir()] == ["frames.sqlite3"]
    store.close()


def test_keyframes_are_published_and_stored_as_sent_and_others_stored_every_interval(store):
    published = []
    fs = FrameStoreEngine(store, Keyframes({2}))
    for n, now in ((1, 0.0), (2, STORE_INTERVAL_S / 2), (3, STORE_INTERVAL_S / 2), (4, STORE_INTERVAL_S * 2)):
        fs.on_input(numbered_input(n), now, published.append)
    no_pose = numbered_frame(5)
    no_pose.ClearField("gimbal_info")
    fs.on_input(Input(ENCODED_FRAME_TYPE, no_pose.SerializeToString(), Id(5)), 9.0, published.append)
    assert [number_of(p) for p in published] == [2]
    assert list(store.unsent()[1]) == [1, 4]


def answering(store, keep=()):
    """A FrameStoreEngine that has stored frames 0-4, posed AWAY, FAR, OFF, SLIGHT and BEST."""
    fs = FrameStoreEngine(store, Keyframes(keep), interval=0)
    for n, pose in enumerate((AWAY, FAR, OFF, SLIGHT, BEST)):
        fs.on_input(numbered_input(n, pose=pose), 0.0, lambda data: None)
    return fs


def test_no_keyframe_is_published_or_even_checked_while_holding(store):
    fs = answering(store, keep={10, 11})
    asked = fs.keyframes.asked
    published = []
    fs.on_status(status(True))
    fs.on_input(numbered_input(10), 1.0, published.append)
    assert published == [] and fs.keyframes.asked == asked
    fs.on_status(status(False))
    fs.on_input(numbered_input(11), 1.0, published.append)
    assert [number_of(p) for p in published] == [11]


def test_a_request_takes_the_best_frame_per_shown_target_at_any_distance_up_to_its_count_then_the_end(store):
    fs = answering(store)
    fs.on_status(status(True, ([WEAK, NOWHERE, WEAK, WEAK], 3)))
    queued = list(fs.queue)
    assert [number_of(p) for p in queued[:-1]] == [4, 1] and queued[-1] == END
    assert 4 not in store.unsent()[1] and 1 not in store.unsent()[1]


def test_a_request_is_started_once_and_a_new_one_starts_again(store):
    fs = answering(store)
    fs.on_status(status(True, ([WEAK], 1)))
    fs.on_status(status(True, ([WEAK], 1)))
    assert len(fs.queue) == 2
    fs.on_status(status(True, ([WEAK, WEAK], 2)))
    assert [None if p == END else number_of(p) for p in fs.queue] == [4, None, 1, 3, None]


def test_frames_are_sent_back_one_at_a_time_each_after_the_last_ones_turn(store):
    fs = answering(store)
    fs.on_status(status(True, ([WEAK, WEAK], 2)))
    sent = []

    def publish(data):
        sent.append(data)
        return Id(len(sent), producer="fs.sendback")

    fs.send_next(publish)
    fs.send_next(publish)
    assert [number_of(p) for p in sent] == [4]
    fs.on_sendback_event(Event(uriel.EventKind.RESULT, Id(99, producer="fs.sendback")))
    fs.send_next(publish)
    assert len(sent) == 1
    fs.on_sendback_event(Event(uriel.EventKind.TIMED_OUT, Id(1, producer="fs.sendback")))
    fs.send_next(publish)
    fs.on_sendback_event(Event(uriel.EventKind.DISCONNECTED))
    fs.send_next(publish)
    assert [number_of(p) for p in sent[:2]] == [4, 1] and sent[2] == END
    fs.on_sendback_event(Event(uriel.EventKind.RESULT, Id(3, producer="fs.sendback")))
    assert not fs.sending_back


def test_no_keyframe_is_published_while_sending_back(store):
    fs = answering(store, keep={10})
    fs.on_status(status(False, ([WEAK], 1)))
    published = []
    fs.on_input(numbered_input(10), 1.0, published.append)
    assert published == []


def test_a_bad_status_raises(store):
    with pytest.raises(ValueError):
        FrameStoreEngine(store, Keyframes(())).on_status(b"{}")


def test_framestore_args_are_defaulted_and_checked():
    args = parse_args(FRAMESTORE_PLUGIN, "")
    assert (args["remote"], args["send_distance"], args["min_disparity"]) == ("swiftmap", 5.0, 40.0)
    for bad in ('{"remote": " "}', '{"send_distance": -1}', '{"min_disparity": -1}'):
        with pytest.raises(ConfigError):
            parse_args(FRAMESTORE_PLUGIN, bad)


MAX_BYTES = 1 << 16
FLOW_SPEC = """
[[producers]]
name = "{camera}"
tokens = 4

[[producers]]
name = "{keyframes}"
tokens = 1

[[producers]]
name = "{sendback}"
tokens = 1

[[engines]]
name = "{framestore}"
location = "local"

[[engines]]
name = "{swiftmap}"
location = "local"

[[edges]]
from = "{camera}"
to = "{framestore}"
timeout = "5s"

[[edges]]
from = "{keyframes}"
to = "{swiftmap}"
timeout = "5s"

[[edges]]
from = "{sendback}"
to = "{swiftmap}"
timeout = "5s"
"""
POSES = [AWAY, BEST, SLIGHT, OFF]
# Two weak targets at the same spot: one frame each, the best then the next best
PLAN = plan_kml((near(0, -15, 10), TARGET), (near(0, -15, 10), TARGET), count=2)


def serve_swiftmap(name, port, stop, ready):
    with Sender(SwiftMapClient("127.0.0.1", port)) as sender:
        remote.run(name, SwiftMap(sender), max_output_size=MAX_BYTES, stop=stop, on_ready=ready.set)


def serve_framestore(name, swiftmap_name, fs, stop, ready):
    framestore.run(name, fs, swiftmap_name, max_output_size=MAX_BYTES, stop=stop, on_ready=ready.set)


def until(condition):
    deadline = time.monotonic() + TIMEOUT_S
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert condition()


@contextlib.contextmanager
def nfn_flow(server, fs):
    """Runs a camera into the framestore `fs`, whose keyframes and sendback
    producers feed a swiftmap engine sending to `server`. Yields send(n),
    which publishes the camera frame numbered n and waits for the framestore to take it."""
    suffix = uuid.uuid4().hex[:8]
    camera, framestore_name, swiftmap_name = f"camera-{suffix}", f"framestore-{suffix}", f"swiftmap-{suffix}"
    keyframes, sendback = producer_name(framestore_name, KEYFRAMES), producer_name(framestore_name, SENDBACK)
    stop = threading.Event()
    readies = [threading.Event() for _ in range(2)]
    threads = [
        threading.Thread(target=serve_swiftmap, daemon=True, args=(swiftmap_name, server.port, stop, readies[0])),
        threading.Thread(target=serve_framestore, daemon=True,
                         args=(framestore_name, swiftmap_name, fs, stop, readies[1])),
    ]
    try:
        for thread, ready in zip(threads, readies):
            thread.start()
            assert ready.wait(TIMEOUT_S)
        with (
            uriel.Server(f"server-{suffix}") as uriel_server,
            uriel.Client(f"client-{suffix}", f"server-{suffix}") as client,
            uriel.Producer(camera, input_type=ENCODED_FRAME_TYPE, max_input_size=MAX_BYTES) as producer,
            uriel.Subscription.engine(framestore_name) as framestore_events,
        ):
            for name in (framestore_name, swiftmap_name):
                uriel_server.add_engine(name)
            for name in (camera, keyframes, sendback):
                client.add_producer(name)
            client.add_flow(f"flow-{suffix}", FLOW_SPEC.format(
                camera=camera, keyframes=keyframes, sendback=sendback,
                framestore=framestore_name, swiftmap=swiftmap_name,
            ))

            def send(n):
                """Publishes camera frame n and waits until the framestore has taken it,
                so a newer frame never replaces it while it waits."""
                seq = producer.send(numbered_frame(n, **POSES[n]).SerializeToString())
                while (event := framestore_events.next(TIMEOUT_S)) is not None:
                    if event.kind == uriel.EventKind.RESULT and event.id.seq == seq:
                        return
                raise AssertionError(f"the framestore never took camera frame {n}")

            yield send
    finally:
        stop.set()
        for thread in threads:
            thread.join(TIMEOUT_S)


def test_keyframes_and_frames_sent_back_reach_the_server_through_the_framestores_producers(server):
    server.replies.extend([NFN_HOLD, PLAN])
    store = FrameStore()
    fs = FrameStoreEngine(store, Keyframes({0}), interval=0)
    try:
        with nfn_flow(server, fs) as send:
            for n in range(4):
                send(n)
            until(lambda: REACHBACK_DONE in server.kinds)
    finally:
        store.close()
    assert numbers_received(server) == [0, 1, 2]
    assert [k for k in server.kinds if k != POLL] == [FRAME, REACHBACK, REACHBACK, REACHBACK_DONE]
