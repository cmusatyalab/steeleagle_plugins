"""Tests of the reachback engine: its store, the frames it picks for each
viewpoint, its file transfers through a stand-in daemon, and frames sent back
to the swiftmap engine end to end through a real Uriel server."""

import math
import threading
import time
import uuid
from concurrent import futures

import grpc
import pytest
import uriel
from conftest import (
    TARGET, Id, Input, near, numbered_frame, numbered_input, numbers_received, plan_kml,
)

from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.services.daemon import transfer_pb2, transfer_pb2_grpc
from swiftmap import engine, reachback
from swiftmap.client import SwiftMapClient
from swiftmap.engine import ENCODED_FRAME_TYPE, Sender, SwiftMap, encode_request
from swiftmap.main import REACHBACK_PLUGIN, ConfigError, parse_args
from swiftmap.reachback import (
    CHUNK_SIZE, ROUTE_HEADER, STORE_INTERVAL_S, FrameStore, Reachback, Transfer,
)

TIMEOUT_S = 10.0
POSE = (40.0, -79.0, 10.0, 0.0, -90.0)


def looking_at(east, north, up, target=TARGET):
    """A frame's pose `east`, `north` and `up` metres from `target`, looking at it."""
    return {"gps": near(east, north, up, target),
            "yaw": math.degrees(math.atan2(-east, -north)),
            "pitch": math.degrees(math.atan2(-up, math.hypot(east, north)))}


IDEAL = looking_at(0, -15, 10)
VIEW = [*IDEAL["gps"], *TARGET]
BEST = looking_at(0, -14, 10)
GOOD = looking_at(8, -13, 10)
AWAY = {**BEST, "yaw": 180.0}
BEHIND = looking_at(0, 15, 10)
FAR = looking_at(0, -40, 10)


@pytest.fixture
def store():
    s = FrameStore()
    yield s
    s.close()


def test_the_store_gives_the_unsent_frames_of_a_stream(store):
    store.add("camera", 1, 5, POSE, b"f5")
    store.add("camera", 1, 6, POSE, b"f6")
    store.add("camera", 2, 7, POSE, b"another session")
    store.add("other", 1, 8, POSE, b"another camera")
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


def test_sessions_above_2_to_the_63_are_stored(store):
    store.add("camera", 1 << 63, 1, POSE, b"f1")
    assert list(store.unsent("camera", 1 << 63)[1]) == [1]


def test_a_frame_with_a_pose_is_stored_every_interval(store):
    rb = Reachback(store, None)
    rb.on_input(numbered_input(1), 0.0)
    rb.on_input(numbered_input(2), STORE_INTERVAL_S / 2)
    rb.on_input(numbered_input(3, session=2), STORE_INTERVAL_S / 2)
    rb.on_input(numbered_input(4), STORE_INTERVAL_S)
    no_pose = numbered_frame(5)
    no_pose.ClearField("gimbal_info")
    rb.on_input(Input(ENCODED_FRAME_TYPE, no_pose.SerializeToString(), Id(5)), 9.0)
    rb.on_input(Input("steeleagle_protocol/v1/messages/frame/RawFrame", b"", Id(6)), 9.0)
    rb.on_input(Input(ENCODED_FRAME_TYPE, b"\xff\xff", Id(7)), 9.0)
    assert list(store.unsent("camera", 1)[1]) == [1, 4]
    assert list(store.unsent("camera", 2)[1]) == [3]


def answering(store):
    sent = []
    rb = Reachback(store, lambda name, data: sent.append(name), interval=0)
    for n, pose in enumerate((AWAY, BEHIND, FAR, GOOD, BEST)):
        rb.on_input(numbered_input(n, pose=pose), 0.0)
    return rb, sent


def test_each_viewpoint_gets_the_frame_looking_at_its_target_from_closest(store):
    rb, sent = answering(store)
    rb.on_request(Id(9), encode_request([VIEW, VIEW, VIEW], set()))
    assert sent == ["reachback-4.frame", "reachback-3.frame"]
    rb.on_request(Id(9), encode_request([VIEW], set()))
    assert len(sent) == 2
    with pytest.raises(ValueError):
        rb.on_request(Id(9), b"{}")


def test_frames_the_asker_has_are_left_out(store):
    rb, sent = answering(store)
    rb.on_request(Id(9), encode_request([VIEW], {4}))
    assert sent == ["reachback-3.frame"]


class FakeDaemon(transfer_pb2_grpc.TransferServiceServicer):
    """Saves the files sent to it, with the route key each came with. It
    refuses files named "refused"."""

    def __init__(self):
        self.files = {}
        self.routes = []

    def TransferFile(self, request_iterator, context):
        self.routes.append(dict(context.invocation_metadata()).get(ROUTE_HEADER))
        name, data = None, b""
        for request in request_iterator:
            name = request.filename
            if name == "refused":
                context.abort(grpc.StatusCode.PERMISSION_DENIED, "not allowed")
            data += request.chunk
            yield transfer_pb2.TransferFileResponse(received=request.number)
        self.files[name] = data


@pytest.fixture
def daemon(tmp_path):
    fake = FakeDaemon()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    transfer_pb2_grpc.add_TransferServiceServicer_to_server(fake, server)
    path = tmp_path / "daemon.sock"
    server.add_insecure_port(f"unix://{path}")
    server.start()
    yield fake, path
    server.stop(None)


def test_files_are_transferred_in_chunks_to_the_remote_daemon(daemon):
    fake, path = daemon
    big = bytes(range(256)) * (CHUNK_SIZE * 5 // 2 // 256)
    with Transfer(str(path), "128.2.208.19") as transfer:
        transfer.send("reachback-1.frame", big)
        transfer.send("refused", b"x")
        transfer.send("reachback-2.frame", b"")
    assert fake.files == {"reachback-1.frame": big, "reachback-2.frame": b""}
    assert fake.routes == ["128.2.208.19"] * 3


def test_reachback_args_are_defaulted():
    assert parse_args(REACHBACK_PLUGIN, "")["remote"] == "swiftmap"
    with pytest.raises(ConfigError):
        parse_args(REACHBACK_PLUGIN, '{"remote": " "}')


MAX_BYTES = 1 << 16
FLOW_SPEC = """
[[producers]]
name = "{producer}"
tokens = 2

[[engines]]
name = "{reachback}"
location = "local"

[[engines]]
name = "{keyframe}"
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
to = "{keyframe}"
timeout = "5s"

[[edges]]
from = "{keyframe}"
to = "{swiftmap}"
timeout = "5s"
"""


def serve_swiftmap(name, port, files_dir, stop, ready):
    with Sender(SwiftMapClient("127.0.0.1", port)) as sender:
        swiftmap = SwiftMap(sender, jpeg_quality=90, files_dir=files_dir)
        engine.run(name, swiftmap, max_output_size=MAX_BYTES, stop=stop, on_ready=ready.set)


def serve_reachback(name, remote, files_dir, stop, ready):
    def transfer(filename, data):
        (files_dir / filename).write_bytes(data)

    store = FrameStore()
    reachback.run(name, Reachback(store, transfer, interval=0), remote, stop=stop,
                  on_ready=ready.set)
    store.close()


def serve_keyframe(name, keep, stop, ready):
    """Forwards only the frames numbered in `keep`, as the keyframe engine would."""
    with uriel.Engine(name, max_output_size=MAX_BYTES) as keyframe:
        ready.set()
        while not stop.is_set():
            input = keyframe.next(0.05)
            if input is None:
                continue
            if frame_pb2.EncodedFrame.FromString(bytes(input.data)).id in keep:
                keyframe.forward()
            else:
                keyframe.drop()


def outcome_of(subscription, seq):
    """What the engine subscribed to did with the input `seq`."""
    while (event := subscription.next(TIMEOUT_S)) is not None:
        if event.kind == uriel.EventKind.RESULT and event.id.seq == seq:
            return event.outcome
    raise AssertionError(f"no result for input {seq}")


def test_the_frame_closest_to_a_planned_viewpoint_is_sent_back_and_mapped(server, tmp_path):
    server.replies.append(plan_kml((IDEAL["gps"], TARGET)))
    suffix = uuid.uuid4().hex[:8]
    producer_name = f"camera-{suffix}"
    reachback_name, keyframe_name, swiftmap_name = (
        f"reachback-{suffix}", f"keyframe-{suffix}", f"swiftmap-{suffix}"
    )
    stop = threading.Event()
    readies = [threading.Event() for _ in range(3)]
    threads = [
        threading.Thread(target=serve_swiftmap, daemon=True,
                         args=(swiftmap_name, server.port, tmp_path, stop, readies[0])),
        threading.Thread(target=serve_keyframe, daemon=True,
                         args=(keyframe_name, {0, 4}, stop, readies[1])),
        threading.Thread(target=serve_reachback, daemon=True,
                         args=(reachback_name, swiftmap_name, tmp_path, stop, readies[2])),
    ]
    poses = [BEST, AWAY, BEST, BEHIND, AWAY]
    try:
        for thread, ready in zip(threads, readies):
            thread.start()
            assert ready.wait(TIMEOUT_S)
        with (
            uriel.Server(f"server-{suffix}") as uriel_server,
            uriel.Client(f"client-{suffix}", f"server-{suffix}") as client,
            uriel.Producer(producer_name, input_type=ENCODED_FRAME_TYPE,
                           max_input_size=MAX_BYTES) as producer,
            uriel.Subscription.engine(keyframe_name) as keyframe_events,
            uriel.Subscription.engine(swiftmap_name) as swiftmap_events,
        ):
            for name in (reachback_name, keyframe_name, swiftmap_name):
                uriel_server.add_engine(name)
            client.add_producer(producer_name)
            client.add_flow(f"flow-{suffix}", FLOW_SPEC.format(
                producer=producer_name, reachback=reachback_name, keyframe=keyframe_name,
                swiftmap=swiftmap_name,
            ))
            for n, pose in enumerate(poses):
                seq = producer.send(numbered_frame(n, **pose).SerializeToString())
                if outcome_of(keyframe_events, seq) == uriel.Outcome.FORWARD:
                    outcome_of(swiftmap_events, seq)
            deadline = time.monotonic() + TIMEOUT_S
            while len(server.received) < 3 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert numbers_received(server) == [0, 4, 2]
            assert list(tmp_path.iterdir()) == []
    finally:
        stop.set()
        for thread in threads:
            thread.join(TIMEOUT_S)
