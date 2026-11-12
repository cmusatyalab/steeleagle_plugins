"""End to end: a frame goes from a real Uriel producer, through a client and
server, to the led-clock engine, which saves an annotated snapshot of it."""

import threading
import time
import uuid

import cv2
import uriel
from conftest import make_frame, make_image, to_gray

from led_clock.decode import LedReader
from led_clock.engine import RAW_FRAME_TYPE, run
from led_clock.frames import CsvWriter, read_rows
from led_clock.latency import LatencyLog
from led_clock.snapshot import JpegSaver

# Room for a 1280x720 RGB frame and its other fields
MAX_BYTES = 4 << 20
TIMEOUT_S = 10.0
TICK_NS = 5_000_000
FPS = 30
INTERVAL_NS = 1
REPORT_INTERVAL_NS = 3_600_000_000_000
FLOW_SPEC = """
[[producers]]
name = "{producer}"
tokens = 2

[[engines]]
name = "{engine}"
location = "local"

[[edges]]
from = "{producer}"
to = "{engine}"
timeout = "5s"
"""


def serve(name, directory, logs, stop, ready):
    log = LatencyLog(
        tick_ns=TICK_NS,
        fps=FPS,
        reader=LedReader(),
        saver=JpegSaver(directory),
        clock=time.monotonic_ns,
        # Never due in the test, so only closing the writer flushes its rows
        report_interval_ns=REPORT_INTERVAL_NS,
        snapshot_interval_ns=INTERVAL_NS,
        writer=CsvWriter(directory / "frames.csv"),
    )
    # Kept, so that garbage collection does not close the writer for the engine
    logs.append(log)
    run(name, log, stop=stop, on_ready=ready.set)


def wait_for_file(path):
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} was not saved in time")


def test_a_received_frame_is_saved_as_a_snapshot_named_by_seq(tmp_path):
    suffix = uuid.uuid4().hex[:8]
    producer_name, engine = f"camera-{suffix}", f"led-clock-{suffix}"
    stop = threading.Event()
    ready = threading.Event()
    logs = []
    thread = threading.Thread(target=serve, args=(engine, tmp_path, logs, stop, ready), daemon=True)
    thread.start()
    assert ready.wait(TIMEOUT_S)
    try:
        with (
            uriel.Server(f"server-{suffix}") as server,
            uriel.Client(f"client-{suffix}", f"server-{suffix}") as client,
            uriel.Producer(
                producer_name, input_type=RAW_FRAME_TYPE, max_input_size=MAX_BYTES
            ) as producer,
        ):
            server.add_engine(engine)
            client.add_producer(producer_name)
            client.add_flow(f"flow-{suffix}", FLOW_SPEC.format(producer=producer_name, engine=engine))
            image = make_image(to_gray(321))
            seq = producer.send(make_frame(image).SerializeToString())
            wait_for_file(tmp_path / f"{seq}.jpeg")
    finally:
        stop.set()
        thread.join(TIMEOUT_S)

    snapshot = cv2.imread(str(tmp_path / f"{seq}.jpeg"))
    assert snapshot.shape == image.shape
    # The engine closes its writer as it stops, so every row is on disk
    assert [row["seq"] for row in read_rows(tmp_path / "frames.csv")] == [seq]
