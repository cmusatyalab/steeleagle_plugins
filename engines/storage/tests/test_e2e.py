"""End to end: frames go through a real Uriel producer, client, server and the
reachback engine, to a stand-in for the remote engine, which asks for a stored
frame with a derived ReachbackRequest."""

import queue
import threading
import time
import uuid

import uriel
from conftest import make_frame

from steeleagle_protocol.v1.messages.frame import frame_pb2
from storage import storage_pb2
from storage.engine import run
from storage.reachback import REACHBACK_REQUEST_TYPE, Reachback
from storage.store import ENCODED_FRAME_TYPE, FrameStore

MAX_BYTES = 1 << 16
TIMEOUT_S = 10.0
FLOW_SPEC = """
[[producers]]
name = "{producer}"
tokens = 2

[[engines]]
name = "{storage}"
location = "local"

[[engines]]
name = "{remote}"
location = "local"

[[edges]]
from = "{producer}"
to = "{storage}"
timeout = "5s"

[[edges]]
from = "{storage}"
to = "{remote}"
timeout = "5s"
"""


def serve_storage(name, remote, stop, ready):
    with FrameStore(":memory:") as store:
        handler = Reachback(store, remote=remote, max_frame_size=MAX_BYTES)
        run(name, handler, [remote], max_output_size=MAX_BYTES, stop=stop, on_ready=ready.set)


def serve_remote(name, flow, stop, ready, requests, received):
    """Puts the frames it gets in `received`, and publishes each (input id,
    request) put in `requests` as a derived result about that input."""
    with uriel.Engine(name, max_output_size=MAX_BYTES) as engine:
        ready.set()
        while not stop.is_set():
            try:
                id, req = requests.get_nowait()
                engine.publish_derived(
                    id, flow, req.SerializeToString(), result_type=REACHBACK_REQUEST_TYPE
                )
            except queue.Empty:
                pass
            input = engine.next(0.05)
            if input is not None:
                received.put(frame_pb2.EncodedFrame.FromString(bytes(input.data)).encoded_data)
                engine.drop()


def test_a_requested_frame_reaches_the_remote_engine():
    suffix = uuid.uuid4().hex[:8]
    producer_name, storage_name, remote_name = (
        f"camera-{suffix}",
        f"storage-{suffix}",
        f"remote-{suffix}",
    )
    flow = f"flow-{suffix}"
    stop = threading.Event()
    requests, received = queue.Queue(), queue.Queue()
    storage_ready, remote_ready = threading.Event(), threading.Event()
    threads = [
        threading.Thread(
            target=serve_remote,
            args=(remote_name, flow, stop, remote_ready, requests, received),
            daemon=True,
        ),
    ]
    threads[0].start()
    assert remote_ready.wait(TIMEOUT_S)
    threads.append(
        threading.Thread(
            target=serve_storage,
            args=(storage_name, remote_name, stop, storage_ready),
            daemon=True,
        )
    )
    threads[1].start()
    assert storage_ready.wait(TIMEOUT_S)

    with (
        uriel.Server(f"server-{suffix}") as server,
        uriel.Client(f"client-{suffix}", f"server-{suffix}") as client,
        uriel.Producer(
            producer_name, input_type=ENCODED_FRAME_TYPE, max_input_size=MAX_BYTES
        ) as producer,
    ):
        server.add_engine(storage_name)
        server.add_engine(remote_name)
        client.add_producer(producer_name)
        client.add_flow(
            flow, FLOW_SPEC.format(producer=producer_name, storage=storage_name, remote=remote_name)
        )
        try:
            # Store frames along a line; none reaches the remote engine yet
            first = None
            for i in range(5):
                seq = producer.send(make_frame(x=float(i), data=f"f{i}".encode()).SerializeToString())
                first = seq if first is None else first
                time.sleep(0.05)
            time.sleep(0.3)
            assert received.empty()

            # The remote engine has had no input, so its request is about the
            # producer's first one
            first_id = uriel.InputId(producer_name, producer.session, first)
            requests.put((first_id, storage_pb2.ReachbackRequest(x=2.9)))
            deadline = time.monotonic() + TIMEOUT_S
            got = None
            while got is None and time.monotonic() < deadline:
                producer.send(make_frame(x=100.0, data=b"carrier").SerializeToString())
                try:
                    got = received.get(timeout=0.2)
                except queue.Empty:
                    pass
            assert got == b"f3"
        finally:
            stop.set()
            for thread in threads:
                thread.join(TIMEOUT_S)
