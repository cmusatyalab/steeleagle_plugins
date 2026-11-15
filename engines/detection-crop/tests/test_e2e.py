"""End to end: frames go from a real Uriel producer, through a client and
server, to a stand-in detection engine and to the detection-crop engine,
which sends their crops across a real remote crossing to a stand-in verifier,
as do the crops of its reintegration producer."""

import queue
import tempfile
import threading
import time
import uuid

import numpy as np
import pytest
import uriel
from conftest import DETECTION_RESULT_TYPE, make_frame, make_result
from steeleagle_protocol.v1.messages.frame import frame_pb2

from detection_crop.crop import RAW_FRAME_TYPE, max_output_size
from detection_crop.cropper import Cropper
from detection_crop.engine import REINTEGRATION, producer_name, run
from detection_crop.send_queue import SendQueue

MAX_BYTES = 1 << 20
TIMEOUT_S = 10.0
FLOW_SPEC = """
[[producers]]
name = "{producer}"
tokens = 2

[[producers]]
name = "{reintegration}"
tokens = 2

[[engines]]
name = "{detector}"
location = "local"

[[engines]]
name = "{crop}"
location = "local"

[[engines]]
name = "{verifier}"
location = "remote"

[[edges]]
from = "{producer}"
to = "{detector}"
timeout = "5s"

[[edges]]
from = "{producer}"
to = "{crop}"
timeout = "5s"

[[edges]]
from = "{crop}"
to = "{verifier}"
timeout = "300ms"

[[edges]]
from = "{reintegration}"
to = "{verifier}"
timeout = "300ms"
"""
# The stand-in detector finds a person in this box and a car in the whole
# frame in seq % 3 == 0 frames; only a car in seq % 3 == 1 ones; and nothing
# (dropping the frame with no result, as detection-qnn does) in the rest.
BOX = (0.25, 0.5, 0.75, 1.0)


def detect(seq):
    if seq % 3 == 0:
        result = make_result(BOX, class_name="person", score=0.6)
        result.MergeFrom(make_result((0, 0, 1, 1), class_name="car", score=0.9))
        return result
    if seq % 3 == 1:
        return make_result((0, 0, 1, 1), class_name="car")
    return None


def serve_detector(name, stop, ready):
    with uriel.Engine(name, result_type=DETECTION_RESULT_TYPE, max_output_size=MAX_BYTES) as engine:
        ready.set()
        while not stop.is_set():
            input = engine.next(0.05)
            if input is None:
                continue
            # Slow enough that the frame reaches the crop engine first
            time.sleep(0.05)
            result = detect(input.id.seq)
            if result is None:
                engine.drop()
            else:
                engine.forward(result=result.SerializeToString())


def serve_verifier(name, received, stop, ready):
    """Stands in for the remote verifier: records every crop it takes."""
    with uriel.Engine(name, max_output_size=MAX_BYTES) as engine:
        ready.set()
        while not stop.is_set():
            input = engine.next(0.02)
            if input is not None:
                frame = frame_pb2.RawFrame.FromString(bytes(input.data))
                received.put((input.payload_type, frame))
                engine.drop()


def start(target, *args):
    ready = threading.Event()
    thread = threading.Thread(target=target, args=(*args, ready), daemon=True)
    thread.start()
    assert ready.wait(TIMEOUT_S)
    return thread


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
        self.server = uriel.Server(
            self.name, bind_addr=f"127.0.0.1:{self.port}", identity_dir=self.identity_dir
        )
        self.server.add_engine(self.engine)

    def close(self):
        self.server.close()


@pytest.fixture
def flow():
    """Starts the engines, and yields (send(frame), cropper, received,
    remote): `received` gets (payload type, RawFrame) of each crop the
    verifier takes."""
    suffix = uuid.uuid4().hex[:8]
    names = {k: f"{k}-{suffix}" for k in ("producer", "detector", "crop", "verifier")}
    names["reintegration"] = producer_name(names["crop"], REINTEGRATION)
    received = queue.Queue()
    stop = threading.Event()
    cropper = Cropper(
        detector=names["detector"],
        class_name="person",
        verifier=names["verifier"],
        queue=SendQueue(max_size=8, resend_after_s=5.0),
        timeout_ns=2_000_000_000,
        max_output_size=max_output_size(320, 240),
        clock=time.monotonic_ns,
    )
    threads = [
        start(serve_detector, names["detector"], stop),
        start(lambda ready: run(names["crop"], cropper, stop=stop, on_ready=ready.set)),
        start(serve_verifier, names["verifier"], received, stop),
    ]
    with tempfile.TemporaryDirectory() as identity_dir:
        remote = Remote(f"remote-{suffix}", names["verifier"], identity_dir)
        try:
            with (
                uriel.Server(f"local-{suffix}") as server,
                uriel.Client(
                    f"client-{suffix}",
                    f"local-{suffix}",
                    remote=uriel.RemoteServer(
                        addr=f"127.0.0.1:{remote.port}", certificate=remote.certificate
                    ),
                ) as client,
                uriel.Producer(
                    names["producer"], input_type=RAW_FRAME_TYPE, max_input_size=MAX_BYTES
                ) as producer,
            ):
                for name in ("detector", "crop"):
                    server.add_engine(names[name])
                client.add_producer(names["producer"])
                client.add_producer(names["reintegration"])
                client.add_flow(f"flow-{suffix}", FLOW_SPEC.format(**names))
                time.sleep(0.3)
                yield producer.send, cropper, received, remote
        finally:
            stop.set()
            for thread in threads:
                thread.join(TIMEOUT_S)
            remote.close()


def wait_until(done):
    deadline = time.monotonic() + TIMEOUT_S
    while not done() and time.monotonic() < deadline:
        time.sleep(0.05)
    return done()


def frame_bytes():
    frame = make_frame(200, 100, color=None)
    frame.id = 42
    return frame.SerializeToString()


def check_crop(payload_type, out):
    assert payload_type.replace(".", "/") == RAW_FRAME_TYPE
    # The person's box, x 100..200 and y 25..75, not the better car
    assert (out.id, out.h_res, out.v_res) == (42, 100, 50)
    pixels = np.frombuffer(out.data, dtype=np.uint8).reshape(50, 100, 3)
    assert tuple(pixels[0, 0]) == (25, 100, 0)


def test_frames_with_the_class_reach_the_verifier_cropped_once(flow):
    send, cropper, received, _ = flow
    seqs = []
    for _ in range(6):
        seqs.append(send(frame_bytes()))
        # One at a time, so the stand-in never skips one
        time.sleep(0.3)
    expected = [s for s in seqs if s % 3 == 0]
    got = [received.get(timeout=TIMEOUT_S) for _ in expected]
    assert wait_until(lambda: cropper.queue.confirmed == len(expected))
    # Nothing is sent again once the verifier has it
    time.sleep(0.5)
    assert received.empty() and len(cropper.queue) == 0
    for payload_type, out in got:
        check_crop(payload_type, out)


def test_a_crop_made_while_the_remote_is_down_reaches_it_once_it_is_back(flow):
    send, cropper, received, remote = flow
    remote.down()
    time.sleep(0.5)
    # Up to the first frame with a person in it; the others are dropped
    while send(frame_bytes()) % 3 != 0:
        time.sleep(0.3)
    time.sleep(1.5)
    assert len(cropper.queue) == 1 and cropper.queue.confirmed == 0
    remote.up()
    # No camera frame comes in: the producer alone gets it there
    assert wait_until(lambda: cropper.queue.confirmed == 1)
    check_crop(*received.get(timeout=TIMEOUT_S))
