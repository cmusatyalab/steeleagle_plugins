"""Unit tests for publishing raw frames and telemetry to Uriel.

These need no drone. Most replace Uriel's producer with a fake; the round
trip tests publish through real Uriel, read back with a Reader.
"""
import time
import uuid

import numpy as np
import pytest
import uriel

import steeleagle_protocol.v1.common.common_pb2 as common_proto
import steeleagle_protocol.v1.messages.frame.frame_pb2 as frame_proto
import steeleagle_protocol.v1.messages.telemetry.telemetry_pb2 as telemetry_proto
import steeleagle_protocol.v1.services.driver.stream_pb2 as stream_proto

from parrot_anafi import raw_frames, stream
from parrot_anafi.stream import Stream

URI = 'harpy.rgb'
HEIGHT, WIDTH = 4, 6
# A pixel's blue, green and red, as OpenCV decodes it.
BGR_PIXEL = (1, 2, 3)
POSITION = telemetry_proto.PositionInfo(
    global_position=common_proto.GlobalPosition(latitude=40.4, longitude=-79.9))
GIMBAL = telemetry_proto.GimbalInfo(pose_body=common_proto.Pose(pitch=-30.0))
# How long a round trip test waits for an input.
READ_WAIT_S = 5.0


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

    def send(self, data):
        assert len(data) <= self.max_input_size
        if self.send_error is not None:
            error, self.send_error = self.send_error, None
            raise error
        self.sent.append(frame_proto.RawFrame.FromString(data))

    def close(self):
        self.closed = True


@pytest.fixture
def producer(monkeypatch):
    FakeProducer.created = []
    monkeypatch.setattr(raw_frames.uriel, 'Producer', FakeProducer)


def frame(height=HEIGHT, width=WIDTH):
    return np.full((height, width, raw_frames.RGB_CHANNELS), BGR_PIXEL, dtype=np.uint8)


def publisher(dimensions=None):
    return raw_frames.RawFramePublisher(URI, dimensions, lambda: (POSITION, GIMBAL))


def unique(name):
    """A producer name no other test run uses, since producers are shared
    across the machine."""
    return f'{name}-{uuid.uuid4().hex[:8]}'


def test_publishes_rgb_frames_with_their_pose(producer):
    raw = publisher()
    raw.publish(frame())
    raw.publish(frame())

    [p] = FakeProducer.created
    assert p.name == URI
    assert p.input_type == 'steeleagle_protocol/v1/messages/frame/RawFrame'
    first, second = p.sent
    assert (first.h_res, first.v_res, first.channels) == (WIDTH, HEIGHT, raw_frames.RGB_CHANNELS)
    assert first.data == bytes(reversed(BGR_PIXEL)) * (HEIGHT * WIDTH)
    assert (first.id, second.id) == (0, 1)
    assert first.position_info == POSITION
    assert first.gimbal_info == GIMBAL
    assert first.HasField('timestamp')


def test_resizes_frames_to_the_requested_dimensions(producer):
    raw = publisher(dimensions=(WIDTH * 2, HEIGHT * 2))
    raw.publish(frame())

    [p] = FakeProducer.created
    [sent] = p.sent
    assert (sent.h_res, sent.v_res) == (WIDTH * 2, HEIGHT * 2)
    assert len(sent.data) == WIDTH * 2 * HEIGHT * 2 * raw_frames.RGB_CHANNELS


def test_drops_a_frame_without_a_free_buffer(producer):
    raw = publisher()
    raw.publish(frame())
    [p] = FakeProducer.created
    p.send_error = uriel.AgainError()

    raw.publish(frame())
    raw.publish(frame())

    assert [sent.id for sent in p.sent] == [0, 2]


def test_recreates_the_producer_for_larger_frames(producer):
    raw = publisher()
    raw.publish(frame())
    raw.publish(frame(height=HEIGHT * 100, width=WIDTH * 100))

    small, large = FakeProducer.created
    assert small.closed
    assert large.max_input_size > small.max_input_size
    assert [sent.v_res for sent in large.sent] == [HEIGHT * 100]


def test_close_closes_the_producer(producer):
    raw = publisher()
    raw.close()  # before any frame, there is nothing to close
    raw.publish(frame())
    raw.close()

    [p] = FakeProducer.created
    assert p.closed


def test_raw_frames_round_trip_through_uriel():
    uri = unique('harpy.rgb')
    reader = uriel.Reader(uri)
    raw = raw_frames.RawFramePublisher(uri, None, lambda: (POSITION, GIMBAL))
    try:
        raw.publish(frame())
        read = reader.next(timeout=READ_WAIT_S)
        assert read is not None
        assert read.payload_type == raw_frames.RAW_FRAME_TYPE
        sent = frame_proto.RawFrame.FromString(bytes(read.data))
        assert (sent.h_res, sent.v_res) == (WIDTH, HEIGHT)
        assert sent.position_info == POSITION
    finally:
        raw.close()
        reader.close()


def test_telemetry_round_trips_through_uriel(monkeypatch):
    uri = unique('harpy.telemetry')
    telemetry = telemetry_proto.Telemetry(
        position_info=POSITION,
        battery_info=telemetry_proto.BatteryInfo(percentage=87),
    )
    monkeypatch.setattr(Stream, 'get_telemetry', lambda self: telemetry)
    s = Stream(drone=None, ip='192.0.2.1')
    reader = uriel.Reader(uri)
    try:
        s.StreamTelemetry(stream_proto.StreamTelemetryRequest(uri=uri), None)
        read = reader.next(timeout=READ_WAIT_S)
        assert read is not None
        assert read.payload_type == stream.TELEMETRY_TYPE
        assert telemetry_proto.Telemetry.FromString(bytes(read.data)) == telemetry
        # A second call replaces the first stream, stopping its thread
        first = s._telemetry_publisher
        s.StreamTelemetry(stream_proto.StreamTelemetryRequest(uri=uri), None)
        assert not first.thread.is_alive()
        assert s._telemetry_publisher is not first
    finally:
        s._telemetry_publisher.stop()
        reader.close()
