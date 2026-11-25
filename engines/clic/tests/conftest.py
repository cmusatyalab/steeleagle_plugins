import numpy as np
import uriel

from steeleagle_protocol.v1.messages.frame import frame_pb2

VERIFIER = "10.0.0.2.verifier"
PRODUCER = "camera"
RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"


def input_id(seq):
    return uriel.InputId(PRODUCER, 1, seq)


def make_event(kind, seq=0, engine=VERIFIER):
    return uriel.Event(
        kind=kind, lost=0, id=input_id(seq), engine=engine, flow="flow", outcome=0,
        payload_type="", result=b"", at_ns=0, offered_at_ns=None, received_at_ns=None,
    )


def make_input(seq, data):
    return uriel.Input(id=input_id(seq), payload_type=RAW_FRAME_TYPE, data=memoryview(data))


def make_frame(width=400, height=300, bright=None, id=0):
    """A black RGB RawFrame, with the box `bright` (x0, y0, x1, y1) white."""
    pixels = np.zeros((height, width, 3), dtype=np.uint8)
    if bright is not None:
        x0, y0, x1, y1 = bright
        pixels[y0:y1, x0:x1] = 255
    return frame_pb2.RawFrame(
        data=pixels.tobytes(), h_res=width, v_res=height, channels=3, id=id, camera_id=3
    ).SerializeToString()


class BrightnessEncoder:
    """Stands in for an image encoder: a tile's embedding is (brightness,
    darkness), normalized. With t = (1, 0) and one background b = (0, 1), a
    white tile scores s = 1 and a black one s = -1."""

    size = 8
    t = np.array([1.0, 0.0])
    b = np.array([[0.0, 1.0]])

    def encode_tiles(self, tiles, batch_size=None):
        brightness = np.array([tile.mean() / 255 for tile in tiles])
        v = np.stack([brightness, 1 - brightness], axis=1)
        return v / np.linalg.norm(v, axis=1, keepdims=True)
