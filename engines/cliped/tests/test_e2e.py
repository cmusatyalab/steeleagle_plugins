"""End to end: frames go through a real Uriel producer, client and server and
the cliped engine, across a real remote crossing to a stand-in verifier.
The image encoder is a stand-in too (conftest.BrightnessEncoder): a white
square in a frame is the target."""

import tempfile
import threading
import time
import uuid

import pytest
import uriel
from conftest import RAW_FRAME_TYPE, BrightnessEncoder, make_frame

from cliped.clip import Selector
from cliped.engine import Cliped, max_output_size, run
from cliped.send_queue import SendQueue
from steeleagle_protocol.v1.messages.frame import frame_pb2

MAX_BYTES = 1 << 20
TIMEOUT_S = 10.0
# Between frames: the camera's rate
FRAME_INTERVAL_S = 0.2
FLOW_SPEC = """
[[producers]]
name = "{producer}"
tokens = 2

[[engines]]
name = "{cliped}"
location = "local"

[[engines]]
name = "{verifier}"
location = "remote"

[[edges]]
from = "{producer}"
to = "{cliped}"
timeout = "2s"

[[edges]]
from = "{cliped}"
to = "{verifier}"
timeout = "300ms"
"""


class Verifier:
    """Stands in for the remote verifier: records the frame id of every tile
    it takes, and takes none while `stalled` is set."""

    def __init__(self, name):
        self.name = name
        self.stalled = threading.Event()
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.frame_ids = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        with uriel.Engine(self.name, max_output_size=MAX_BYTES) as engine:
            self.ready.set()
            while not self.stop.is_set():
                if self.stalled.is_set():
                    time.sleep(0.02)
                    continue
                input = engine.next(0.02)
                if input is not None:
                    self.frame_ids.append(frame_pb2.RawFrame.FromString(bytes(input.data)).id)
                    engine.drop()


class Remote:
    """The remote daemon's server, which can go away and come back at the same
    address with the same identity, as a restarted daemon would."""

    def __init__(self, name, engine, identity_dir):
        self.name, self.engine, self.identity_dir = name, engine, identity_dir
        self.server = uriel.Server(name, bind_addr="127.0.0.1:0", identity_dir=identity_dir)
        self.server.add_engine(engine)
        self.port = self.server.remote_port
        self.certificate = self.server.certificate

    def down(self):
        self.server.close()

    def up(self):
        self.server = uriel.Server(self.name, bind_addr=f"127.0.0.1:{self.port}", identity_dir=self.identity_dir)
        self.server.add_engine(self.engine)

    def close(self):
        self.server.close()


@pytest.fixture
def flow():
    """Starts cliped and the verifier, and yields (send(frame), cliped,
    verifier, remote)."""
    suffix = uuid.uuid4().hex[:8]
    names = {k: f"{k}-{suffix}" for k in ("producer", "cliped", "verifier")}
    encoder = BrightnessEncoder()
    cliped = Cliped(
        encoder,
        Selector(encoder.t, encoder.b),
        SendQueue(max_size=8, resend_after_s=5.0),
        prompt="a white square",
        verifier=names["verifier"],
        tile_size=200,
        stride=150,
    )
    stop, ready = threading.Event(), threading.Event()
    engine = threading.Thread(
        target=run,
        args=(names["cliped"], cliped),
        kwargs={"max_output_size": max_output_size(200), "stop": stop, "on_ready": ready.set},
        daemon=True,
    )
    engine.start()
    verifier = Verifier(names["verifier"])
    assert ready.wait(TIMEOUT_S) and verifier.ready.wait(TIMEOUT_S)

    with tempfile.TemporaryDirectory() as identity_dir:
        remote = Remote(f"remote-{suffix}", names["verifier"], identity_dir)
        try:
            with (
                uriel.Server(f"local-{suffix}") as server,
                uriel.Client(
                    f"client-{suffix}",
                    f"local-{suffix}",
                    remote=uriel.RemoteServer(addr=f"127.0.0.1:{remote.port}", certificate=remote.certificate),
                ) as client,
                uriel.Producer(names["producer"], input_type=RAW_FRAME_TYPE, max_input_size=MAX_BYTES) as producer,
            ):
                server.add_engine(names["cliped"])
                client.add_producer(names["producer"])
                client.add_flow(f"flow-{suffix}", FLOW_SPEC.format(**names))
                time.sleep(0.3)
                yield producer.send, cliped, verifier, remote
        finally:
            stop.set()
            verifier.stop.set()
            engine.join(TIMEOUT_S)
            verifier.thread.join(TIMEOUT_S)
            remote.close()


def send_until(send, done, frame_id):
    """Sends empty frames, from `frame_id` on, until `done()` or the time runs
    out; each carries a queued tile, if one waits."""
    deadline = time.monotonic() + TIMEOUT_S
    while not done() and time.monotonic() < deadline:
        send(make_frame(id=frame_id))
        frame_id += 1
        time.sleep(FRAME_INTERVAL_S)
    return done()


def test_a_target_tile_reaches_the_verifier_once(flow):
    send, cliped, verifier, _ = flow
    send(make_frame(bright=(150, 0, 350, 200), id=7))
    assert send_until(send, lambda: cliped.queue.confirmed == 1, 8)
    # Empty frames carry nothing once the verifier has it
    for frame_id in range(100, 105):
        send(make_frame(id=frame_id))
        time.sleep(FRAME_INTERVAL_S)
    assert verifier.frame_ids == [7] and len(cliped.queue) == 0


def test_a_stalled_verifier_does_not_lose_the_tile(flow):
    send, cliped, verifier, _ = flow
    verifier.stalled.set()
    send(make_frame(bright=(150, 0, 350, 200), id=7))
    # Its server takes the tile (ACCEPTED), the verifier does not, and the
    # offer times out: the tile stays queued, and is sent again
    for frame_id in range(8, 14):
        send(make_frame(id=frame_id))
        time.sleep(FRAME_INTERVAL_S)
    assert len(cliped.queue) == 1 and cliped.queue.confirmed == 0 and verifier.frame_ids == []
    verifier.stalled.clear()
    assert send_until(send, lambda: cliped.queue.confirmed == 1, 14)
    assert set(verifier.frame_ids) == {7}


def test_a_tile_found_while_the_remote_is_down_reaches_it_once_it_is_back(flow):
    send, cliped, verifier, remote = flow
    remote.down()
    time.sleep(0.5)
    send(make_frame(bright=(150, 0, 350, 200), id=7))
    for frame_id in range(8, 14):
        send(make_frame(id=frame_id))
        time.sleep(FRAME_INTERVAL_S)
    assert len(cliped.queue) == 1 and cliped.queue.confirmed == 0
    remote.up()
    assert send_until(send, lambda: cliped.queue.confirmed == 1, 14)
    assert set(verifier.frame_ids) == {7}
