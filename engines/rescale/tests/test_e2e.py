"""End to end: frames go through a real Uriel producer, client, server and the
rescale engine, to a stand-in for the engine after it."""

import queue
import threading
import uuid

import numpy as np
import uriel

from rescale.engine import run
from rescale.rescale import RAW_FRAME_TYPE
from steeleagle_protocol.v1.messages.frame import frame_pb2

MAX_BYTES = 1 << 20
TIMEOUT_S = 10.0
DIMENSIONS = (320, 180)
FLOW_SPEC = """
[[producers]]
name = "{producer}"
tokens = 2

[[engines]]
name = "{rescale}"
location = "local"

[[engines]]
name = "{sink}"
location = "local"

[[edges]]
from = "{producer}"
to = "{rescale}"
timeout = "5s"

[[edges]]
from = "{rescale}"
to = "{sink}"
timeout = "5s"
"""


def frame(width, height, id):
    return frame_pb2.RawFrame(
        data=np.full((height, width, 3), 99, dtype=np.uint8).tobytes(),
        h_res=width, v_res=height, channels=3, id=id,
    ).SerializeToString()


def serve_sink(name, stop, ready, received):
    with uriel.Engine(name, max_output_size=MAX_BYTES) as engine:
        ready.set()
        while not stop.is_set():
            input = engine.next(0.05)
            if input is not None:
                received.put((input.payload_type, frame_pb2.RawFrame.FromString(bytes(input.data))))
                engine.drop()


def test_frames_reach_the_next_engine_rescaled():
    suffix = uuid.uuid4().hex[:8]
    producer_name, rescale_name, sink_name = f"camera-{suffix}", f"rescale-{suffix}", f"sink-{suffix}"
    stop = threading.Event()
    received = queue.Queue()
    sink_ready, rescale_ready = threading.Event(), threading.Event()
    threads = [
        threading.Thread(target=serve_sink, args=(sink_name, stop, sink_ready, received), daemon=True),
        threading.Thread(
            target=run, args=(rescale_name, DIMENSIONS),
            kwargs={"stop": stop, "on_ready": rescale_ready.set}, daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    assert sink_ready.wait(TIMEOUT_S) and rescale_ready.wait(TIMEOUT_S)

    with (
        uriel.Server(f"server-{suffix}") as server,
        uriel.Client(f"client-{suffix}", f"server-{suffix}") as client,
        uriel.Producer(producer_name, input_type=RAW_FRAME_TYPE, max_input_size=MAX_BYTES) as producer,
    ):
        server.add_engine(rescale_name)
        server.add_engine(sink_name)
        client.add_producer(producer_name)
        client.add_flow(
            f"flow-{suffix}",
            FLOW_SPEC.format(producer=producer_name, rescale=rescale_name, sink=sink_name),
        )
        try:
            # One frame to rescale, one already at the resolution
            for id, (width, height) in enumerate([(640, 360), DIMENSIONS]):
                producer.send(frame(width, height, id))
                payload_type, out = received.get(timeout=TIMEOUT_S)
                assert payload_type.replace(".", "/") == RAW_FRAME_TYPE
                assert (out.id, out.h_res, out.v_res) == (id, *DIMENSIONS)
                assert len(out.data) == DIMENSIONS[0] * DIMENSIONS[1] * 3
        finally:
            stop.set()
            for thread in threads:
                thread.join(TIMEOUT_S)
