"""Tests of the reachback engine: its store, its answers to requests, its file
transfers through a stand-in daemon, and lost frames sent back to the swiftmap
engine end to end through a real Uriel server."""

import threading
import time
import uuid
from concurrent import futures

import grpc
import pytest
import uriel
from conftest import Id, Input, numbered_frame, numbered_input, numbers_received

from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.services.daemon import transfer_pb2, transfer_pb2_grpc
from swiftmap import engine, reachback
from swiftmap.client import SwiftMapClient
from swiftmap.engine import ENCODED_FRAME_TYPE, Sender, SwiftMap, encode_request
from swiftmap.main import REACHBACK_PLUGIN, ConfigError, parse_args
from swiftmap.reachback import CHUNK_SIZE, ROUTE_HEADER, FrameStore, Reachback, Transfer

TIMEOUT_S = 10.0


@pytest.fixture
def store():
    s = FrameStore()
    yield s
    s.close()


def test_the_store_gives_a_range_of_frames_in_order(store):
    for number in (5, 3, 4, 9):
        store.add("camera", 1, number, f"f{number}".encode())
    store.add("camera", 1, 4, b"f4 again")
    store.add("camera", 2, 4, b"another session")
    store.add("other", 1, 4, b"another camera")
    assert store.frames("camera", 1, 3, 5) == [(3, b"f3"), (4, b"f4 again"), (5, b"f5")]
    assert store.frames("camera", 1, 6, 8) == []


def test_sessions_above_2_to_the_63_are_stored(store):
    store.add("camera", 1 << 63, 1, b"f1")
    assert store.frames("camera", 1 << 63, 1, 1) == [(1, b"f1")]


def test_frames_are_stored_and_requests_answered_with_files(store):
    sent = []
    rb = Reachback(store, lambda name, data: sent.append((name, data)))
    for n in range(1, 7):
        rb.on_input(numbered_input(n))
    rb.on_input(numbered_input(4, session=2))
    rb.on_request(Id(99), encode_request(3, 4))
    assert [name for name, _ in sent] == ["reachback-3.frame", "reachback-4.frame"]
    assert frame_pb2.EncodedFrame.FromString(sent[0][1]) == numbered_frame(3)
    with pytest.raises(ValueError):
        rb.on_request(Id(99), b"{}")


def test_inputs_that_are_not_encoded_frames_are_not_stored(store):
    rb = Reachback(store, None)
    rb.on_input(Input("steeleagle_protocol/v1/messages/frame/RawFrame", b"", Id(1)))
    rb.on_input(Input(ENCODED_FRAME_TYPE, b"\xff\xff", Id(2)))
    assert store.frames("camera", 1, 0, 10) == []


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
        # A refused file is logged, and the next still goes
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
name = "{lossy}"
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
to = "{lossy}"
timeout = "5s"

[[edges]]
from = "{lossy}"
to = "{swiftmap}"
timeout = "5s"
"""


def serve_swiftmap(name, port, files_dir, stop, ready):
    with Sender(SwiftMapClient("127.0.0.1", port)) as sender:
        swiftmap = SwiftMap(sender, jpeg_quality=90, gap_timeout=TIMEOUT_S, files_dir=files_dir)
        engine.run(name, swiftmap, max_output_size=MAX_BYTES, stop=stop, on_ready=ready.set)


def serve_reachback(name, remote, files_dir, stop, ready):
    def transfer(filename, data):
        # As the daemon would save it, for the swiftmap engine to find
        (files_dir / filename).write_bytes(data)

    store = FrameStore()
    reachback.run(name, Reachback(store, transfer), remote, stop=stop, on_ready=ready.set)
    store.close()


def serve_lossy(name, lost, stop, ready):
    """Loses the frames numbered in `lost`, as the network would."""
    with uriel.Engine(name, max_output_size=MAX_BYTES) as lossy:
        ready.set()
        while not stop.is_set():
            input = lossy.next(0.05)
            if input is None:
                continue
            if frame_pb2.EncodedFrame.FromString(bytes(input.data)).id in lost:
                lossy.drop()
            else:
                lossy.forward()


def outcome_of(subscription, seq):
    """What the engine subscribed to did with the input `seq`."""
    while (event := subscription.next(TIMEOUT_S)) is not None:
        if event.kind == uriel.EventKind.RESULT and event.id.seq == seq:
            return event.outcome
    raise AssertionError(f"no result for input {seq}")


def test_lost_frames_are_sent_back_and_mapped_in_order(server, tmp_path):
    suffix = uuid.uuid4().hex[:8]
    producer_name = f"camera-{suffix}"
    reachback_name, lossy_name, swiftmap_name = (
        f"reachback-{suffix}", f"lossy-{suffix}", f"swiftmap-{suffix}"
    )
    stop = threading.Event()
    readies = [threading.Event() for _ in range(3)]
    threads = [
        threading.Thread(target=serve_swiftmap, daemon=True,
                         args=(swiftmap_name, server.port, tmp_path, stop, readies[0])),
        threading.Thread(target=serve_lossy, daemon=True,
                         args=(lossy_name, {3, 4}, stop, readies[1])),
        # Last, as it subscribes to the swiftmap engine
        threading.Thread(target=serve_reachback, daemon=True,
                         args=(reachback_name, swiftmap_name, tmp_path, stop, readies[2])),
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
            uriel.Subscription.engine(lossy_name) as lossy_events,
            uriel.Subscription.engine(swiftmap_name) as swiftmap_events,
        ):
            for name in (reachback_name, lossy_name, swiftmap_name):
                uriel_server.add_engine(name)
            client.add_producer(producer_name)
            client.add_flow(f"flow-{suffix}", FLOW_SPEC.format(
                producer=producer_name, reachback=reachback_name, lossy=lossy_name,
                swiftmap=swiftmap_name,
            ))
            # One at a time, so none is skipped for a newer one
            for n in range(8):
                seq = producer.send(numbered_frame(n).SerializeToString())
                if outcome_of(lossy_events, seq) == uriel.Outcome.FORWARD:
                    outcome_of(swiftmap_events, seq)
            deadline = time.monotonic() + TIMEOUT_S
            while len(server.received) < 8 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert numbers_received(server) == list(range(8))
            assert list(tmp_path.iterdir()) == []
    finally:
        stop.set()
        for thread in threads:
            thread.join(TIMEOUT_S)
