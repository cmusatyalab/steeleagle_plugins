"""End to end: frames go from a real Uriel producer, through a client and
server, to a stand-in detection engine and to the detection-crop engine,
which sends their crops to a stand-in for the engine after it."""

import queue
import threading
import time
import uuid

import numpy as np
import uriel
from conftest import DETECTION_RESULT_TYPE, make_frame, make_result
from steeleagle_protocol.v1.messages.frame import frame_pb2

from detection_crop.crop import RAW_FRAME_TYPE, max_output_size
from detection_crop.cropper import Cropper
from detection_crop.engine import run

MAX_BYTES = 1 << 20
TIMEOUT_S = 10.0
FLOW_SPEC = """
[[producers]]
name = "{producer}"
tokens = 2

[[engines]]
name = "{detector}"
location = "local"

[[engines]]
name = "{crop}"
location = "local"

[[engines]]
name = "{sink}"
location = "local"

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
to = "{sink}"
timeout = "5s"
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
    with uriel.Engine(
        name, result_type=DETECTION_RESULT_TYPE, max_output_size=MAX_BYTES
    ) as engine:
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


def serve_crop(name, detector, stop, ready):
    cropper = Cropper(
        detector=detector,
        class_name="person",
        timeout_ns=2_000_000_000,
        max_output_size=max_output_size(320, 240),
        clock=time.monotonic_ns,
    )
    run(name, cropper, stop=stop, on_ready=ready.set)


def serve_sink(name, received, stop, ready):
    with uriel.Engine(name, max_output_size=MAX_BYTES) as engine:
        ready.set()
        while not stop.is_set():
            input = engine.next(0.05)
            if input is not None:
                frame = frame_pb2.RawFrame.FromString(bytes(input.data))
                received.put((input.id.seq, input.payload_type, frame))
                engine.drop()


def start(target, *args):
    ready = threading.Event()
    thread = threading.Thread(target=target, args=(*args, ready), daemon=True)
    thread.start()
    assert ready.wait(TIMEOUT_S)
    return thread


def test_frames_with_the_class_reach_the_next_engine_cropped():
    suffix = uuid.uuid4().hex[:8]
    producer_name = f"camera-{suffix}"
    detector, crop, sink = f"detector-{suffix}", f"crop-{suffix}", f"sink-{suffix}"
    received = queue.Queue()
    stop = threading.Event()
    threads = [
        start(serve_detector, detector, stop),
        start(serve_crop, crop, detector, stop),
        start(serve_sink, sink, received, stop),
    ]
    try:
        with (
            uriel.Server(f"server-{suffix}") as server,
            uriel.Client(f"client-{suffix}", f"server-{suffix}") as client,
            uriel.Producer(
                producer_name, input_type=RAW_FRAME_TYPE, max_input_size=MAX_BYTES
            ) as producer,
        ):
            for name in (detector, crop, sink):
                server.add_engine(name)
            client.add_producer(producer_name)
            client.add_flow(
                f"flow-{suffix}",
                FLOW_SPEC.format(
                    producer=producer_name, detector=detector, crop=crop, sink=sink
                ),
            )
            frame = make_frame(200, 100, color=None)
            frame.id = 42
            seqs = []
            for _ in range(6):
                seqs.append(producer.send(frame.SerializeToString()))
                # One at a time, so the stand-in never skips one
                time.sleep(0.3)
            expected = [s for s in seqs if s % 3 == 0]
            got = [received.get(timeout=TIMEOUT_S) for _ in expected]
            time.sleep(0.5)
            assert received.empty()
    finally:
        stop.set()
        for thread in threads:
            thread.join(TIMEOUT_S)

    assert [seq for seq, _, _ in got] == expected
    for _, payload_type, out in got:
        assert payload_type.replace(".", "/") == RAW_FRAME_TYPE
        # The person's box, x 100..200 and y 25..75, not the better car
        assert (out.id, out.h_res, out.v_res) == (42, 100, 50)
        pixels = np.frombuffer(out.data, dtype=np.uint8).reshape(50, 100, 3)
        assert tuple(pixels[0, 0]) == (25, 100, 0)
