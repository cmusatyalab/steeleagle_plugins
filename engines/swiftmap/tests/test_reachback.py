"""Tests of the reachback engine: its store, the frames it picks for the weak
targets of a request, keyframe selection pausing while they are sent back,
sending again what does not arrive, and frames sent back through a real Uriel
flow to the swiftmap engine."""

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
from swiftmap import reachback, remote
from swiftmap.protocol import (
    ENCODED_FRAME_TYPE, REACHBACK_DONE_TYPE, REACHBACK_FRAME_TYPE, STATUS_TYPE, Request,
    encode_status, unstamp,
)
from swiftmap.remote import (
    FRAME, NFN_HOLD, NFN_RELEASE, POLL, REACHBACK, REACHBACK_DONE, Sender, SwiftMap, SwiftMapClient,
)
from swiftmap.main import REACHBACK_PLUGIN, ConfigError, parse_args
from swiftmap.reachback import (
    DROP, FORWARD, LINK_S, MAX_TRIES, REPORT_TIMEOUT_S, STORE_INTERVAL_S, FrameStore, Reachback,
)

RESULT, SKIPPED, TIMED_OUT = (
    uriel.EventKind.RESULT, uriel.EventKind.SKIPPED, uriel.EventKind.TIMED_OUT
)

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


@dataclass
class Event:
    kind: uriel.EventKind
    id: Id = None
    payload_type: str = ""
    result: bytes = b""


def tell(rb, hold, request=None, now=1.0):
    """Has the swiftmap engine tell `rb` the NFN status at `now`."""
    rb.on_event(Event(uriel.EventKind.DERIVED, Id(9), STATUS_TYPE, encode_status(hold, request)), now)


def report(rb, kind, *seqs):
    """Has uriel say the inputs `seqs` came to `kind` at the swiftmap engine."""
    for seq in seqs:
        rb.on_event(Event(kind, Id(seq)), 1.0)


def unstamped(action):
    """(output type, request id, number, frame number or None) of what was
    sent back."""
    output_type, payload = action
    request_id, number, data = unstamp(payload)
    return output_type, request_id, number, number_of(data) if data else None


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
    assert store.read(rowids[0]) == b"f5"
    assert list(store.unsent("camera", 1)[1]) == [5, 6]
    store.mark_sent(rowids[0])
    assert list(store.unsent("camera", 1)[1]) == [6]
    assert len(store.unsent("camera", 3)[0]) == 0


def test_only_the_newest_frames_are_kept():
    store = FrameStore(keep=3)
    for number in range(5):
        store.add("camera", 1, number, POSE, b"")
    assert list(store.unsent("camera", 1)[1]) == [2, 3, 4]
    assert store.read(1) is None
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
    rb = answering(store, keep={22, 23})
    tell(rb, False, Request(7, [WEAK, WEAK], 2))
    sent = [rb.on_input(numbered_input(n), 1.0) for n in (20, 21)]
    assert [unstamped(a) for a in sent] == [(REACHBACK_FRAME_TYPE, 7, 0, 4),
                                             (REACHBACK_FRAME_TYPE, 7, 1, 3)]
    # The end waits for word that both arrived; keyframes go meanwhile
    assert rb.on_input(numbered_input(22), 1.0) == FORWARD
    report(rb, RESULT, 20, 21)
    assert unstamped(rb.on_input(numbered_input(24), 1.0)) == (REACHBACK_DONE_TYPE, 7, 2, None)
    report(rb, RESULT, 24)
    assert not rb.busy
    assert rb.on_input(numbered_input(23), 1.0) == FORWARD
    unsent = store.unsent("camera", 1)[1]
    assert 20 in unsent and 3 not in unsent and 4 not in unsent


def test_a_request_sends_at_most_its_count_and_the_end_even_with_none(store):
    rb = answering(store)
    tell(rb, True, Request(1, [WEAK], 5))
    assert [out.output_type for out in rb.todo] == [REACHBACK_FRAME_TYPE] * 2
    for out in rb.todo:
        store.mark_sent(out.rowid)
    tell(rb, True, Request(2, [WEAK], 5))
    assert not rb.todo
    assert unstamped(rb.on_input(numbered_input(20), 1.0)) == (REACHBACK_DONE_TYPE, 2, 0, None)
    with pytest.raises(ValueError):
        rb.on_status(Id(9), b"{}", 1.0)


def test_a_request_is_started_once_and_dropped_when_withdrawn(store):
    rb = answering(store)
    tell(rb, True, Request(1, [WEAK], 1))
    assert rb.on_input(numbered_input(20), 1.0)[0] == REACHBACK_FRAME_TYPE
    # The status repeats it: not started over, so the end still waits for 20
    tell(rb, True, Request(1, [WEAK], 1))
    assert rb.on_input(numbered_input(21), 1.0) == DROP
    tell(rb, True, None)
    assert not rb.busy
    report(rb, TIMED_OUT, 20)
    assert rb.on_input(numbered_input(22), 1.0) == DROP
    # A late status naming it does not start it again
    tell(rb, True, Request(1, [WEAK], 1))
    assert not rb.busy


def test_a_hold_stops_keyframes_until_released_but_not_frames_sent_back(store):
    rb = answering(store, keep={20})
    tell(rb, True)
    assert rb.on_input(numbered_input(20), 1.0) == DROP
    assert 20 in store.unsent("camera", 1)[1]
    tell(rb, True, Request(1, [WEAK], 1))
    assert rb.on_input(numbered_input(21), 1.0)[0] == REACHBACK_FRAME_TYPE
    report(rb, RESULT, 21)
    assert rb.on_input(numbered_input(22), 1.0)[0] == REACHBACK_DONE_TYPE
    report(rb, RESULT, 22)
    assert rb.on_input(numbered_input(20), 1.0) == DROP
    tell(rb, False)
    assert rb.on_input(numbered_input(20), 1.0) == FORWARD


def test_a_frame_that_does_not_arrive_is_sent_again_up_to_max_tries(store):
    rb = answering(store)
    tell(rb, True, Request(1, [WEAK], 1))
    sent = rb.on_input(numbered_input(100), 1.0)
    for seq in range(100, 100 + MAX_TRIES - 1):
        report(rb, TIMED_OUT, seq)
        assert rb.on_input(numbered_input(seq + 1), 1.0) == sent
    report(rb, SKIPPED, 100 + MAX_TRIES - 1)
    # Given up: the end goes next
    assert rb.on_input(numbered_input(200), 1.0)[0] == REACHBACK_DONE_TYPE


def test_a_frame_that_arrives_late_is_not_sent_again(store):
    rb = answering(store)
    tell(rb, True, Request(1, [WEAK], 1))
    rb.on_input(numbered_input(20), 1.0)
    report(rb, TIMED_OUT, 20)
    report(rb, RESULT, 20)
    assert rb.on_input(numbered_input(21), 1.0)[0] == REACHBACK_DONE_TYPE


def test_a_lost_link_sends_again_without_using_up_tries(store):
    rb = answering(store)
    tell(rb, True, Request(1, [WEAK], 1))
    sent = rb.on_input(numbered_input(20), 1.0)
    for seq in range(21, 21 + 2 * MAX_TRIES):
        rb.on_event(Event(uriel.EventKind.DISCONNECTED), 1.0)
        assert rb.on_input(numbered_input(seq), 1.0) == sent


def test_nothing_is_sent_back_while_no_status_comes(store):
    rb = answering(store)
    tell(rb, True, Request(1, [WEAK], 1), now=0.0)
    assert rb.on_input(numbered_input(20), LINK_S) == DROP
    tell(rb, True, Request(1, [WEAK], 1), now=10.0)
    assert rb.on_input(numbered_input(21), 10.0)[0] == REACHBACK_FRAME_TYPE


def test_what_has_no_word_for_a_while_is_sent_again(store):
    rb = answering(store)
    tell(rb, True, Request(1, [WEAK], 1))
    sent = rb.on_input(numbered_input(20), 1.0)
    later = 1.0 + REPORT_TIMEOUT_S
    tell(rb, True, Request(1, [WEAK], 1), now=later)
    rb.expire(later - 0.1)
    assert rb.on_input(numbered_input(21), later) == DROP
    rb.expire(later)
    assert rb.on_input(numbered_input(22), later) == sent


def test_the_end_is_sent_again_until_it_arrives(store):
    rb = answering(store)
    tell(rb, True, Request(1, [[41.0, -80.0, 0.0]], 1))
    for seq in range(20, 20 + 2 * MAX_TRIES):
        assert rb.on_input(numbered_input(seq), 1.0)[0] == REACHBACK_DONE_TYPE
        report(rb, TIMED_OUT, seq)
    rb.on_input(numbered_input(50), 1.0)
    report(rb, RESULT, 50)
    assert not rb.busy


def test_a_frame_over_max_size_is_not_sent_back(store):
    rb = Reachback(store, Keyframes(()), interval=0, max_size=16)
    rb.on_input(numbered_input(0, pose=BEST), 0.0)
    tell(rb, True, Request(1, [WEAK], 1))
    assert not rb.todo and rb.end is not None


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
tokens = {tokens}

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
timeout = "{timeout}"
"""
POSES = [AWAY, BEST, SLIGHT, OFF]
PLAN = plan_kml((near(0, -15, 10), TARGET), count=2)


class StallingSwiftMap(SwiftMap):
    """Takes `stall_s` over the first frame sent back, as a slow link would."""

    def __init__(self, sender, *, stall_s):
        super().__init__(sender)
        self.stall_s = stall_s

    def on_input(self, input):
        if self.stall_s and input.payload_type == REACHBACK_FRAME_TYPE:
            time.sleep(self.stall_s)
            self.stall_s = 0
        super().on_input(input)


class RecordingReachback(Reachback):
    """Records the number of each frame it sends back, and "end" for the end."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent_back = []

    def on_input(self, input, now):
        action = super().on_input(input, now)
        if isinstance(action, tuple):
            output_type, payload = action
            self.sent_back.append(
                unstamp(payload)[1] if output_type == REACHBACK_FRAME_TYPE else "end")
        return action


def serve_swiftmap(name, port, stop, ready, stall_s):
    with Sender(SwiftMapClient("127.0.0.1", port)) as sender:
        swiftmap = StallingSwiftMap(sender, stall_s=stall_s)
        remote.run(name, swiftmap, max_output_size=MAX_BYTES, stop=stop, on_ready=ready.set)


def serve_reachback(name, remote, rb, stop, ready):
    reachback.run(name, rb, remote, max_output_size=MAX_BYTES, stop=stop, on_ready=ready.set)


def outcome_of(subscription, seq):
    """What the engine subscribed to did with the input `seq`."""
    while (event := subscription.next(TIMEOUT_S)) is not None:
        if event.kind == uriel.EventKind.RESULT and event.id.seq == seq:
            return event.outcome
    raise AssertionError(f"no result for input {seq}")


def until(condition):
    deadline = time.monotonic() + TIMEOUT_S
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert condition()


@contextlib.contextmanager
def nfn_flow(server, rb, *, tokens=2, timeout="5s", stall_s=0.0):
    """Runs a flow from a camera through `rb` to a swiftmap engine that sends
    to `server` and takes `stall_s` over the first frame sent back. Yields
    send(n, wait=True), which sends the frame numbered n, then if `wait`,
    waits for what reachback did with it and the swiftmap engine's result."""
    suffix = uuid.uuid4().hex[:8]
    producer_name, reachback_name, swiftmap_name = (
        f"camera-{suffix}", f"reachback-{suffix}", f"swiftmap-{suffix}"
    )
    stop = threading.Event()
    readies = [threading.Event() for _ in range(2)]
    threads = [
        threading.Thread(target=serve_swiftmap, daemon=True,
                         args=(swiftmap_name, server.port, stop, readies[0], stall_s)),
        threading.Thread(target=serve_reachback, daemon=True,
                         args=(reachback_name, swiftmap_name, rb, stop, readies[1])),
    ]
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
                tokens=tokens, timeout=timeout,
            ))

            def send(n, wait=True):
                pose = POSES[n] if n < len(POSES) else AWAY
                seq = producer.send(numbered_frame(n, **pose).SerializeToString())
                if wait and outcome_of(reachback_events, seq) != uriel.Outcome.DROP:
                    outcome_of(swiftmap_events, seq)

            yield send
    finally:
        stop.set()
        for thread in threads:
            thread.join(TIMEOUT_S)


def send_until_done(send, server, first, wait=True):
    """Sends frames numbered from `first` until the server has the end of
    what is sent back: frames carry what is sent back, and the end goes once
    word comes that the frames arrived."""
    n, deadline = first, time.monotonic() + TIMEOUT_S
    while REACHBACK_DONE not in server.kinds and time.monotonic() < deadline:
        send(n, wait)
        n += 1
        if not wait:
            time.sleep(0.1)
    assert REACHBACK_DONE in server.kinds


def test_an_nfn_round_holds_keyframes_and_sends_frames_back_through_the_flow(server):
    server.replies.extend([NFN_HOLD, PLAN])
    store = FrameStore()
    rb = Reachback(store, Keyframes({0, 2, 99}), interval=0)
    try:
        with nfn_flow(server, rb) as send:
            send(0)
            until(lambda: rb.holding)
            for n in (1, 2, 3):
                send(n)
            until(lambda: rb.busy)
            send_until_done(send, server, first=4)
            server.replies.append(NFN_RELEASE)
            until(lambda: not rb.holding)
            send(99)
            until(lambda: len(server.received) == 4)
    finally:
        store.close()
    assert numbers_received(server) == [0, 1, 2, 99]
    assert [k for k in server.kinds if k != POLL] == [
        FRAME, REACHBACK, REACHBACK, REACHBACK_DONE, FRAME]
    assert POLL in server.kinds


def test_a_frame_sent_back_that_times_out_is_sent_again_and_reaches_the_server_once(server):
    server.replies.extend([NFN_HOLD, PLAN])
    store = FrameStore()
    rb = RecordingReachback(store, Keyframes({0}), interval=0)
    try:
        with nfn_flow(server, rb, tokens=8, timeout="200ms", stall_s=1.2) as send:
            send(0)
            until(lambda: rb.holding)
            for n in (1, 2, 3):
                send(n)
            until(lambda: rb.busy)
            send_until_done(send, server, first=4, wait=False)
    finally:
        store.close()
    # The first frame sent back timed out while the swiftmap engine stalled,
    # so it went again; the engine passed it on once all the same
    assert rb.sent_back.count(0) > 1
    received = numbers_received(server)
    assert received[:2] == [0, 1] and len(received) == len(set(received))
    assert server.kinds.count(REACHBACK_DONE) == 1
