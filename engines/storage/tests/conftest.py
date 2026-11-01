from dataclasses import dataclass

import pytest
import uriel

from steeleagle_protocol.v1.common import common_pb2
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.telemetry import telemetry_pb2
from storage.store import ENCODED_FRAME_TYPE, FrameStore

PRODUCER = "camera"
SESSION = 0xDEADBEEF_CAFEF00D  # above 2**63, as uint64 sessions can be
REMOTE = "100.64.0.2.detection"
CLASSIFIER = "100.64.0.1.classifier"


def make_frame(
    lat=None, lon=None, alt=None, heading=None, x=None, y=None, z=None, angle=None, data=b"h264"
):
    frame = frame_pb2.EncodedFrame(encoded_data=data, codec=frame_pb2.CODEC_H264)
    position = telemetry_pb2.PositionInfo()
    g = common_pb2.GlobalPosition()
    for field, value in (("latitude", lat), ("longitude", lon), ("altitude", alt), ("heading", heading)):
        if value is not None:
            setattr(g, field, value)
    r = common_pb2.RelativePosition()
    for field, value in (("x", x), ("y", y), ("z", z), ("angle", angle)):
        if value is not None:
            setattr(r, field, value)
    if g.ListFields():
        position.global_position.CopyFrom(g)
    if r.ListFields():
        position.relative_position.CopyFrom(r)
    if position.ListFields():
        frame.position_info.CopyFrom(position)
    return frame


@dataclass(frozen=True)
class FakeInput:
    id: uriel.InputId
    payload_type: str
    data: bytes


def make_input(seq, frame=None, payload_type=ENCODED_FRAME_TYPE):
    frame = frame if frame is not None else make_frame()
    return FakeInput(
        uriel.InputId(PRODUCER, SESSION, seq), payload_type, frame.SerializeToString()
    )


def make_event(kind, engine, seq=0, result=b"", payload_type=""):
    return uriel.Event(
        kind=kind,
        lost=0,
        id=uriel.InputId(PRODUCER, SESSION, seq),
        engine=engine,
        flow="flow",
        outcome=uriel.Outcome.DROP,
        payload_type=payload_type,
        result=result,
        at_ns=0,
        offered_at_ns=None,
        received_at_ns=None,
    )


@pytest.fixture
def store():
    with FrameStore(":memory:") as store:
        yield store
