import numpy as np
import uriel
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.result import result_pb2

PRODUCER = "camera"
SESSION = 0xDEADBEEF_CAFEF00D  # above 2**63, as uint64 sessions can be
DETECTOR = "100.64.0.1.detection"
DETECTION_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/DetectionResult"
RGB_CHANNELS = 3


def input_id(seq):
    return uriel.InputId(PRODUCER, SESSION, seq)


def make_frame(width=64, height=48, color=(0, 0, 0)):
    """A solid frame, or, with color None, one whose pixel (y, x) is
    (y, x, 0), so a crop shows where it was cut from."""
    pixels = np.empty((height, width, RGB_CHANNELS), dtype=np.uint8)
    if color is None:
        ys, xs = np.indices((height, width))
        pixels[..., 0], pixels[..., 1], pixels[..., 2] = ys, xs, 0
    else:
        pixels[:] = color
    return frame_pb2.RawFrame(
        data=pixels.tobytes(), h_res=width, v_res=height, channels=RGB_CHANNELS
    )


def make_result(*boxes, class_name="person", score=0.9):
    """A DetectionResult with a detection of each normalized
    (y_min, x_min, y_max, x_max) box."""
    result = result_pb2.DetectionResult()
    for y_min, x_min, y_max, x_max in boxes:
        result.detections.add(
            class_name=class_name,
            score=score,
            bbox=result_pb2.BoundingBox(
                y_min=y_min, x_min=x_min, y_max=y_max, x_max=x_max
            ),
        )
    return result


def make_event(
    kind,
    seq=0,
    result=b"",
    engine=DETECTOR,
    outcome=uriel.Outcome.FORWARD,
    payload_type=DETECTION_RESULT_TYPE,
):
    return uriel.Event(
        kind=kind,
        lost=0,
        id=input_id(seq),
        engine=engine,
        flow="flow",
        outcome=outcome,
        payload_type=payload_type if result else "",
        result=result,
        at_ns=0,
        offered_at_ns=None,
        received_at_ns=None,
    )


class Clock:
    def __init__(self):
        self.ns = 1_000_000_000

    def __call__(self):
        return self.ns

    def advance_ms(self, ms):
        self.ns += ms * 1_000_000


