"""End to end: frames go through a real Uriel producer, client and server,
an encoder and its decoder, to a stand-in for the engine after them."""

import queue
import threading
import uuid

import numpy as np
import pytest
import uriel

from steeleagle_protocol.v1.messages.frame import frame_pb2
from thumbnail import main
from thumbnail.frames import RAW_FRAME_TYPE

MAX_BYTES = 1 << 20
TIMEOUT_S = 10.0
FLOW_SPEC = """
[[producers]]
name = "{producer}"
tokens = 2

[[engines]]
name = "{encoder}"
location = "local"

[[engines]]
name = "{decoder}"
location = "local"

[[engines]]
name = "{sink}"
location = "local"

[[edges]]
from = "{producer}"
to = "{encoder}"
timeout = "5s"

[[edges]]
from = "{encoder}"
to = "{decoder}"
timeout = "5s"

[[edges]]
from = "{decoder}"
to = "{sink}"
timeout = "5s"
"""


def serve_sink(name, ready, received, stop):
    with uriel.Engine(name, max_output_size=MAX_BYTES) as engine:
        ready.set()
        while not stop.is_set():
            input = engine.next(0.05)
            if input is not None:
                received.put((input.payload_type, frame_pb2.RawFrame.FromString(bytes(input.data))))
                engine.drop()


def serve(plugin, name, ready):
    args = main.parse_args(plugin, f'{{"max_output_size": {MAX_BYTES}}}')
    # The engines serve until the process ends; `ready` is set once they could
    # have registered, which the first frame through confirms.
    ready.set()
    main.runner(plugin)(name, args)


@pytest.mark.parametrize(
    "encoder_plugin, decoder_plugin, tolerance",
    [
        (main.WEBP_ENCODER_PLUGIN, main.WEBP_DECODER_PLUGIN, 6),
        (main.JPEG_ENCODER_PLUGIN, main.JPEG_DECODER_PLUGIN, 6),
    ],
)
def test_frames_come_out_of_the_decoder_as_they_went_in(encoder_plugin, decoder_plugin, tolerance):
    suffix = uuid.uuid4().hex[:8]
    producer_name, encoder_name, decoder_name, sink_name = (
        f"camera-{suffix}", f"encoder-{suffix}", f"decoder-{suffix}", f"sink-{suffix}"
    )
    stop = threading.Event()
    received = queue.Queue()
    ready = [threading.Event() for _ in range(3)]
    threads = [
        threading.Thread(target=serve_sink, args=(sink_name, ready[0], received, stop), daemon=True),
        threading.Thread(target=serve, args=(encoder_plugin, encoder_name, ready[1]), daemon=True),
        threading.Thread(target=serve, args=(decoder_plugin, decoder_name, ready[2]), daemon=True),
    ]
    for thread in threads:
        thread.start()
    assert all(event.wait(TIMEOUT_S) for event in ready)

    width, height = 96, 64
    pixels = np.zeros((height, width, 3), dtype=np.uint8)
    pixels[..., 0] = 200
    pixels[height // 2 :, :, 1] = 120
    sent = frame_pb2.RawFrame(data=pixels.tobytes(), h_res=width, v_res=height, channels=3, id=41)

    with (
        uriel.Server(f"server-{suffix}") as server,
        uriel.Client(f"client-{suffix}", f"server-{suffix}") as client,
        uriel.Producer(producer_name, input_type=RAW_FRAME_TYPE, max_input_size=MAX_BYTES) as producer,
    ):
        for name in (encoder_name, decoder_name, sink_name):
            server.add_engine(name)
        client.add_producer(producer_name)
        client.add_flow(
            f"flow-{suffix}",
            FLOW_SPEC.format(producer=producer_name, encoder=encoder_name, decoder=decoder_name, sink=sink_name),
        )
        try:
            # The engines may still be registering: send until a frame is through
            out = None
            for _ in range(int(TIMEOUT_S / 0.5)):
                producer.send(sent.SerializeToString())
                try:
                    payload_type, out = received.get(timeout=0.5)
                    break
                except queue.Empty:
                    continue
            assert out is not None, "no frame came out of the decoder"
            assert payload_type.replace(".", "/") == RAW_FRAME_TYPE
            assert (out.h_res, out.v_res, out.channels) == (width, height, 3)
            decoded = np.frombuffer(out.data, np.uint8).reshape(height, width, 3)
            assert np.abs(decoded.astype(int) - pixels).mean() < tolerance
        finally:
            stop.set()
