"""Unit tests for publishing raw frames to Gabriel.

These need no drone: Gabriel is replaced by a fake module, since only eagled
has its wheel.
"""
import sys
import types

import numpy as np
import pytest

import steeleagle_protocol.v1.common.common_pb2 as common_proto
import steeleagle_protocol.v1.messages.telemetry.telemetry_pb2 as telemetry_proto

from parrot_anafi import raw_frames

PRODUCER = 'harpy.raw_frames'
FLOW = 'harpy'
HEIGHT, WIDTH = 4, 6
# A pixel's blue, green and red, as OpenCV decodes it.
BGR_PIXEL = (1, 2, 3)
POSITION = telemetry_proto.PositionInfo(
    global_position=common_proto.GlobalPosition(latitude=40.4, longitude=-79.9))
GIMBAL = telemetry_proto.GimbalInfo(pose_body=common_proto.Pose(pitch=-30.0))


class FakeAgainError(Exception):
    pass


class FakeProducer:
    """Keeps what it is sent, or raises `send_error` once."""

    created = []

    def __init__(self, name, *, input_type, max_input_size):
        self.name = name
        self.input_type = input_type
        self.max_input_size = max_input_size
        self.sent = []
        self.closed = False
        self.send_error = None
        FakeProducer.created.append(self)

    def send(self, flow, data):
        assert len(data) <= self.max_input_size
        if self.send_error is not None:
            error, self.send_error = self.send_error, None
            raise error
        self.sent.append((flow, telemetry_proto.RawFrame.FromString(data)))

    def close(self):
        self.closed = True


@pytest.fixture
def gabriel(monkeypatch):
    FakeProducer.created = []
    module = types.SimpleNamespace(Producer=FakeProducer, AgainError=FakeAgainError)
    monkeypatch.setitem(sys.modules, 'gabriel', module)
    return module


def frame(height=HEIGHT, width=WIDTH):
    return np.full((height, width, raw_frames.RGB_CHANNELS), BGR_PIXEL, dtype=np.uint8)


def publisher():
    return raw_frames.RawFramePublisher(PRODUCER, FLOW, lambda: (POSITION, GIMBAL))


def test_publishes_rgb_frames_with_their_pose(gabriel):
    raw = publisher()
    raw.publish(frame())
    raw.publish(frame())

    [producer] = FakeProducer.created
    assert producer.name == PRODUCER
    assert producer.input_type == 'steeleagle_protocol.v1.messages.telemetry.RawFrame'
    assert [flow for flow, _ in producer.sent] == [FLOW, FLOW]
    first, second = (sent for _, sent in producer.sent)
    assert (first.h_res, first.v_res, first.channels) == (WIDTH, HEIGHT, raw_frames.RGB_CHANNELS)
    assert first.data == bytes(reversed(BGR_PIXEL)) * (HEIGHT * WIDTH)
    assert (first.id, second.id) == (0, 1)
    assert first.position_info == POSITION
    assert first.gimbal_info == GIMBAL
    assert first.HasField('timestamp')


def test_drops_a_frame_without_a_free_buffer(gabriel):
    raw = publisher()
    raw.publish(frame())
    [producer] = FakeProducer.created
    producer.send_error = FakeAgainError()

    raw.publish(frame())
    raw.publish(frame())

    assert [sent.id for _, sent in producer.sent] == [0, 2]


def test_recreates_the_producer_for_larger_frames(gabriel):
    raw = publisher()
    raw.publish(frame())
    raw.publish(frame(height=HEIGHT * 100, width=WIDTH * 100))

    small, large = FakeProducer.created
    assert small.closed
    assert large.max_input_size > small.max_input_size
    assert [sent.v_res for _, sent in large.sent] == [HEIGHT * 100]


def test_close_closes_the_producer(gabriel):
    raw = publisher()
    raw.close()  # before any frame, there is nothing to close
    raw.publish(frame())
    raw.close()

    [producer] = FakeProducer.created
    assert producer.closed


def test_nothing_is_published_without_eagled_asking(gabriel, monkeypatch):
    monkeypatch.delenv(raw_frames.RAW_FRAMES_PRODUCER_ENV, raising=False)
    monkeypatch.delenv(raw_frames.FLOW_ENV, raising=False)
    assert raw_frames.from_environment(lambda: (POSITION, GIMBAL)) is None


def test_publishes_where_eagled_says(gabriel, monkeypatch):
    monkeypatch.setenv(raw_frames.RAW_FRAMES_PRODUCER_ENV, PRODUCER)
    monkeypatch.setenv(raw_frames.FLOW_ENV, FLOW)
    raw = raw_frames.from_environment(lambda: (POSITION, GIMBAL))
    raw.publish(frame())

    [producer] = FakeProducer.created
    assert producer.name == PRODUCER
    assert [flow for flow, _ in producer.sent] == [FLOW]


def test_nothing_is_published_without_gabriel(monkeypatch):
    monkeypatch.setenv(raw_frames.RAW_FRAMES_PRODUCER_ENV, PRODUCER)
    monkeypatch.setenv(raw_frames.FLOW_ENV, FLOW)
    # A None entry makes importing the module raise ImportError.
    monkeypatch.setitem(sys.modules, 'gabriel', None)
    assert raw_frames.from_environment(lambda: (POSITION, GIMBAL)) is None
