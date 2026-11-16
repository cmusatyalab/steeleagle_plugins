"""End to end: frames go from a real Uriel producer, through a client and
server, to a stand-in detection engine and to the detection-log engine, which
saves them with the stand-in's results drawn."""

import threading
import time
import uuid

import numpy as np
import uriel
from conftest import DETECTION_RESULT_TYPE, make_frame, make_result
from PIL import Image

from detection_log.engine import run
from detection_log.frames import FrameSaver
from detection_log.log import RAW_FRAME_TYPE, DetectionLog

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
name = "{log}"
location = "local"

[[edges]]
from = "{producer}"
to = "{detector}"
timeout = "5s"

[[edges]]
from = "{producer}"
to = "{log}"
timeout = "5s"
"""
# The stand-in detector finds this box in even frames, and nothing (dropping
# the frame with no result, as detection-qnn does) in odd ones.
BOX = (0.25, 0.25, 0.75, 0.75)


def serve_detector(name, stop, ready):
    with uriel.Engine(
        name, result_type=DETECTION_RESULT_TYPE, max_output_size=MAX_BYTES
    ) as engine:
        ready.set()
        while not stop.is_set():
            input = engine.next(0.05)
            if input is None:
                continue
            # Slow enough that the frame reaches the log engine first
            time.sleep(0.05)
            if input.id.seq % 2 == 0:
                engine.forward(result=make_result(BOX).SerializeToString())
            else:
                engine.drop()


def serve_log(name, detector, directory, stop, ready):
    log = DetectionLog(
        detector=detector,
        saver=FrameSaver(directory),
        timeout_ns=2_000_000_000,
        clock=time.monotonic_ns,
    )
    run(name, log, stop=stop, on_ready=ready.set)


def start(target, *args):
    ready = threading.Event()
    thread = threading.Thread(target=target, args=(*args, ready), daemon=True)
    thread.start()
    assert ready.wait(TIMEOUT_S)
    return thread


def wait_for_files(directory, count):
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        if directory.is_dir() and len(list(directory.iterdir())) >= count:
            return
        time.sleep(0.05)
    raise AssertionError(f"{count} frames were not saved in time")


def test_frames_are_saved_named_by_seq_with_the_detectors_boxes(tmp_path):
    suffix = uuid.uuid4().hex[:8]
    producer_name = f"camera-{suffix}"
    detector, log = f"detector-{suffix}", f"log-{suffix}"
    directory = tmp_path / "detection_log"
    stop = threading.Event()
    threads = [
        start(serve_detector, detector, stop),
        start(serve_log, log, detector, directory, stop),
    ]
    try:
        with (
            uriel.Server(f"server-{suffix}") as server,
            uriel.Client(f"client-{suffix}", f"server-{suffix}") as client,
            uriel.Producer(
                producer_name, input_type=RAW_FRAME_TYPE, max_input_size=MAX_BYTES
            ) as producer,
        ):
            for name in (detector, log):
                server.add_engine(name)
            client.add_producer(producer_name)
            client.add_flow(
                f"flow-{suffix}",
                FLOW_SPEC.format(producer=producer_name, detector=detector, log=log),
            )
            frame = make_frame(200, 200, color=(255, 255, 255)).SerializeToString()
            seqs = []
            for _ in range(2):
                seqs.append(producer.send(frame))
                # One at a time, so the stand-in never skips one
                time.sleep(0.3)
            wait_for_files(directory / "out", len(seqs))
    finally:
        stop.set()
        for thread in threads:
            thread.join(TIMEOUT_S)

    expected = sorted(f"{s}.jpeg" for s in seqs)
    for subdirectory in ("raw", "out"):
        assert sorted(p.name for p in (directory / subdirectory).iterdir()) == expected
    for seq in seqs:
        with Image.open(directory / "out" / f"{seq}.jpeg") as image:
            pixels = np.asarray(image.convert("RGB"), dtype=int)
        # The box's left side, at x = 50, is red only where it was found
        r, g, b = pixels[100, 51]
        boxed = r > 150 and g < 100 and b < 100
        assert boxed == (seq % 2 == 0)
