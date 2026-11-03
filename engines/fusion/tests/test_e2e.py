"""End to end: frames go from a real Uriel producer, through a client and
server, to stand-ins for a local and a remote detection engine and to the
fusion engine, whose results are read from its events."""

import threading
import time
import uuid

import uriel
from conftest import DETECTION_RESULT_TYPE

from fusion.detection import DetectionFusion
from fusion.engine import run

MAX_BYTES = 1 << 16
TIMEOUT_S = 10.0
GRACE_MS = 150
TIMEOUT_MS = 1000
RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"
FLOW_SPEC = """
[[producers]]
name = "{producer}"
tokens = 2

[[engines]]
name = "{local}"
location = "local"

[[engines]]
name = "{remote}"
location = "local"

[[engines]]
name = "{fusion}"
location = "local"

[[edges]]
from = "{producer}"
to = "{local}"
timeout = "5s"

[[edges]]
from = "{producer}"
to = "{remote}"
timeout = "5s"

[[edges]]
from = "{producer}"
to = "{fusion}"
timeout = "5s"
"""


def serve_detector(name, label, delays, stop, ready):
    """Forwards each frame after `delays[label]` seconds, with b"<label>:<frame>"
    as its result, or drops it with no result if the delay is None."""
    with uriel.Engine(
        name, result_type=DETECTION_RESULT_TYPE, max_output_size=MAX_BYTES
    ) as engine:
        ready.set()
        while not stop.is_set():
            input = engine.next(0.05)
            if input is None:
                continue
            delay = delays[label]
            if delay is None:
                engine.drop()
                continue
            time.sleep(delay)
            engine.forward(result=label.encode() + b":" + bytes(input.data))


def serve_fusion(name, local, remote, stop, ready):
    handler = DetectionFusion(
        local=local,
        remote=remote,
        grace_ns=GRACE_MS * 1_000_000,
        timeout_ns=TIMEOUT_MS * 1_000_000,
        clock=time.monotonic_ns,
    )
    run(
        name,
        handler,
        [local, remote],
        result_type=DETECTION_RESULT_TYPE,
        max_output_size=MAX_BYTES,
        stop=stop,
        on_ready=ready.set,
    )


def start(target, *args):
    ready = threading.Event()
    thread = threading.Thread(target=target, args=(*args, ready), daemon=True)
    thread.start()
    assert ready.wait(TIMEOUT_S)
    return thread


def fused(events, seq):
    """The fusion engine's RESULT event for the input `seq`."""
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        event = events.next(0.1)
        if event is not None and event.kind == uriel.EventKind.RESULT and event.id.seq == seq:
            return event
    raise AssertionError(f"no fused result for input {seq}")


def test_fusion_prefers_a_timely_remote_result():
    suffix = uuid.uuid4().hex[:8]
    producer_name = f"camera-{suffix}"
    local, remote, fusion = f"local-{suffix}", f"remote-{suffix}", f"fusion-{suffix}"
    flow = f"flow-{suffix}"
    stop = threading.Event()
    delays = {"local": 0.0, "remote": 0.0}
    threads = [
        start(serve_detector, local, "local", delays, stop),
        start(serve_detector, remote, "remote", delays, stop),
        start(serve_fusion, fusion, local, remote, stop),
    ]
    try:
        with (
            uriel.Server(f"server-{suffix}") as server,
            uriel.Client(f"client-{suffix}", f"server-{suffix}") as client,
            uriel.Producer(
                producer_name, input_type=RAW_FRAME_TYPE, max_input_size=MAX_BYTES
            ) as producer,
            uriel.Subscription.engine(fusion) as events,
        ):
            for name in (local, remote, fusion):
                server.add_engine(name)
            client.add_producer(producer_name)
            client.add_flow(
                flow,
                FLOW_SPEC.format(
                    producer=producer_name, local=local, remote=remote, fusion=fusion
                ),
            )

            def send(delays_now, frame):
                delays.update(delays_now)
                started = time.monotonic()
                event = fused(events, producer.send(frame))
                assert event.outcome == uriel.Outcome.DROP
                return event.result, time.monotonic() - started

            # The remote result comes within the grace period
            got, _ = send({"local": 0.0, "remote": 0.05}, b"f1")
            assert got == b"remote:f1"

            # It comes too late: the local result, once the grace period ends
            got, took = send({"local": 0.0, "remote": 0.6}, b"f2")
            assert got == b"local:f2"
            assert GRACE_MS / 1000 <= took < 0.5
            time.sleep(0.6)  # until the remote engine is free again

            # The remote engine drops the frame: the local result at once
            got, took = send({"local": 0.0, "remote": None}, b"f3")
            assert got == b"local:f3"
            assert took < GRACE_MS / 1000

            # The local engine drops it: the remote result, however long after
            got, _ = send({"local": None, "remote": 0.3}, b"f4")
            assert got == b"remote:f4"

            # Both drop it: dropped with no result
            got, _ = send({"local": None, "remote": None}, b"f5")
            assert got == b""
    finally:
        stop.set()
        for thread in threads:
            thread.join(TIMEOUT_S)
