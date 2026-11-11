"""Unit tests for reading the drone's video stream and the StreamService.

These need no drone: the video stream is replaced by fake captures, and
frames by fake publishers.
"""
import threading

import grpc
import numpy as np
import pytest

import steeleagle_protocol.v1.services.driver.stream_pb2 as stream_proto

from parrot_anafi import stream
from parrot_anafi.stream import Stream

# How long a test waits for the reading thread.
THREAD_WAIT_S = 5.0
FRAME = np.zeros((2, 2, 3), dtype=np.uint8)
URI = 'harpy.rgb'


class StopReading(Exception):
    """Ends the reading thread's loop, which otherwise runs for good."""


class FakeCapture:
    """Gives `reads` in order, each a frame or None for a failed read."""

    def __init__(self, opened=True, reads=()):
        self._opened = opened
        self._reads = list(reads)
        self.released = False

    def isOpened(self):
        return self._opened

    def read(self):
        frame = self._reads.pop(0) if self._reads else None
        return frame is not None, frame

    def release(self):
        self.released = True


class FakePublisher:
    created = []

    def __init__(self, uri, dimensions, pose):
        self.uri = uri
        self.dimensions = dimensions
        self.published = []
        self.closed = False
        FakePublisher.created.append(self)

    def publish(self, frame):
        self.published.append(frame)

    def close(self):
        self.closed = True


class FakeContext:
    """Raises on abort, as gRPC does."""

    def abort(self, code, details):
        self.code = code
        raise grpc.RpcError(details)


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(stream, 'CAPTURE_RETRY_DELAY', 0)


@pytest.fixture(autouse=True)
def fake_publisher(monkeypatch):
    FakePublisher.created = []
    monkeypatch.setattr(stream, 'RawFramePublisher', FakePublisher)


def opening(monkeypatch, *captures):
    """Makes the stream open `captures` in order, then stop reading."""
    remaining = list(captures)

    def open_capture(self):
        if not remaining:
            raise StopReading
        return remaining.pop(0)

    monkeypatch.setattr(Stream, '_open_capture', open_capture)


def camera_request(uri=URI, camera=stream.CAMERA_NAME, resolution=None):
    request = stream_proto.CameraRequest(id=camera, uri=uri)
    if resolution is not None:
        request.target_resolution = resolution
    return stream_proto.StreamCamerasRequest(primary=request)


def streaming(uri=URI, resolution=None):
    """A Stream that the vehicle asked to stream its camera as `uri`, without
    starting its reading thread, so that a test can run it."""
    s = Stream(drone=None, ip='192.0.2.1')
    s._start_capture = lambda: None
    s.StreamCameras(camera_request(uri, resolution=resolution), FakeContext())
    return s


# StopReading ends the reading thread, which pytest reports.
@pytest.mark.filterwarnings('ignore::pytest.PytestUnhandledThreadExceptionWarning')
def test_streaming_cameras_does_not_wait_for_the_video_stream(monkeypatch):
    opened = threading.Event()
    stream_ready = threading.Event()

    def open_capture(self):
        opened.set()
        stream_ready.wait()
        raise StopReading

    monkeypatch.setattr(Stream, '_open_capture', open_capture)
    s = Stream(drone=None, ip='192.0.2.1')
    s.StreamCameras(camera_request(), FakeContext())

    # The stream is still opening, yet StreamCameras has returned.
    assert opened.wait(THREAD_WAIT_S)
    stream_ready.set()
    s._grabber_thread.join(THREAD_WAIT_S)


def test_the_video_stream_is_read_only_once_asked_for():
    s = Stream(drone=None, ip='192.0.2.1')
    assert s._grabber_thread is None


def test_a_stream_that_fails_to_open_is_retried(monkeypatch):
    failed = FakeCapture(opened=False)
    working = FakeCapture(reads=[FRAME])
    opening(monkeypatch, failed, working)
    s = streaming()

    with pytest.raises(StopReading):
        s._grab_frames()

    assert failed.released and working.released
    [publisher] = FakePublisher.created
    assert len(publisher.published) == 1


def test_a_stream_that_stops_giving_frames_is_reopened(monkeypatch):
    stopped = FakeCapture(reads=[FRAME] + [None] * stream.MAX_FAILED_READS)
    reopened = FakeCapture(reads=[FRAME])
    opening(monkeypatch, stopped, reopened)
    s = streaming()

    with pytest.raises(StopReading):
        s._grab_frames()

    assert stopped.released and reopened.released
    [publisher] = FakePublisher.created
    assert len(publisher.published) == 2
    assert publisher.closed


def test_a_few_failed_reads_keep_the_stream(monkeypatch):
    reads = [FRAME] + [None] * (stream.MAX_FAILED_READS - 1) + [FRAME]
    opening(monkeypatch, FakeCapture(reads=reads))
    s = streaming()

    with pytest.raises(StopReading):
        s._grab_frames()

    [publisher] = FakePublisher.created
    assert len(publisher.published) == 2


def test_frames_are_published_where_and_how_the_vehicle_asks(monkeypatch):
    opening(monkeypatch, FakeCapture(reads=[FRAME]))
    s = streaming(resolution=stream_proto.RESOLUTION_480P)

    with pytest.raises(StopReading):
        s._grab_frames()

    [publisher] = FakePublisher.created
    assert publisher.uri == URI
    assert publisher.dimensions == (854, 480)


class AskingCapture(FakeCapture):
    """Gives two frames, calling `between` after reading the first."""

    def __init__(self, between):
        super().__init__(reads=[FRAME, FRAME])
        self._between = between
        self._count = 0

    def read(self):
        if self._count == 1:
            self._between()
        self._count += 1
        return super().read()


def test_a_new_request_replaces_the_publisher(monkeypatch):
    s = streaming()
    opening(monkeypatch, AskingCapture(
        lambda: s.StreamCameras(camera_request('other.rgb'), FakeContext())))

    with pytest.raises(StopReading):
        s._grab_frames()

    first, second = FakePublisher.created
    assert first.uri == URI and first.closed and len(first.published) == 1
    assert second.uri == 'other.rgb' and len(second.published) == 1


def test_asking_for_no_camera_stops_publishing(monkeypatch):
    s = streaming()
    opening(monkeypatch, AskingCapture(
        lambda: s.StreamCameras(stream_proto.StreamCamerasRequest(), FakeContext())))

    with pytest.raises(StopReading):
        s._grab_frames()

    [publisher] = FakePublisher.created
    assert publisher.closed and len(publisher.published) == 1


def test_unknown_cameras_are_refused():
    s = Stream(drone=None, ip='192.0.2.1')
    context = FakeContext()
    with pytest.raises(grpc.RpcError):
        s.StreamCameras(camera_request(camera='thermal'), context)
    assert context.code == grpc.StatusCode.INVALID_ARGUMENT
    assert s._grabber_thread is None


def test_telemetry_needs_a_uri():
    s = Stream(drone=None, ip='192.0.2.1')
    context = FakeContext()
    with pytest.raises(grpc.RpcError):
        s.StreamTelemetry(stream_proto.StreamTelemetryRequest(), context)
    assert context.code == grpc.StatusCode.INVALID_ARGUMENT
