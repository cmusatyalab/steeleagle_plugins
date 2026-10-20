"""Unit tests for reading the drone's video stream.

These need no drone: the video stream is replaced by fake captures.
"""
import threading

import numpy as np
import pytest

from parrot_anafi import stream
from parrot_anafi.stream import Stream

# How long a test waits for the reading thread.
THREAD_WAIT_S = 5.0
FRAME = np.zeros((2, 2, 3), dtype=np.uint8)


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
    def __init__(self):
        self.published = []
        self.closed = False

    def publish(self, frame):
        self.published.append(frame)

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(stream, 'CAPTURE_RETRY_DELAY', 0)


def opening(monkeypatch, *captures):
    """Makes the stream open `captures` in order, then stop reading."""
    remaining = list(captures)

    def open_capture(self):
        if not remaining:
            raise StopReading
        return remaining.pop(0)

    monkeypatch.setattr(Stream, '_open_capture', open_capture)


# StopReading ends the reading thread, which pytest reports.
@pytest.mark.filterwarnings('ignore::pytest.PytestUnhandledThreadExceptionWarning')
def test_starting_does_not_wait_for_the_video_stream(monkeypatch):
    publisher = FakePublisher()
    monkeypatch.setattr(stream.raw_frames, 'from_environment', lambda pose: publisher)
    opened = threading.Event()
    stream_ready = threading.Event()

    def open_capture(self):
        opened.set()
        stream_ready.wait()
        raise StopReading

    monkeypatch.setattr(Stream, '_open_capture', open_capture)
    s = Stream(drone=None, ip='192.0.2.1')

    # The stream is still opening, yet Stream() has returned.
    assert opened.wait(THREAD_WAIT_S)
    stream_ready.set()
    s._grabber_thread.join(THREAD_WAIT_S)
    assert publisher.closed


def test_a_stream_that_fails_to_open_is_retried(monkeypatch):
    failed = FakeCapture(opened=False)
    working = FakeCapture(reads=[FRAME])
    opening(monkeypatch, failed, working)
    s = Stream(drone=None, ip='192.0.2.1')

    with pytest.raises(StopReading):
        s._grab_frames()

    assert failed.released and working.released
    assert s._read_latest_frame()[0]


def test_a_stream_that_stops_giving_frames_is_reopened(monkeypatch):
    stopped = FakeCapture(reads=[FRAME] + [None] * stream.MAX_FAILED_READS)
    reopened = FakeCapture(reads=[FRAME])
    opening(monkeypatch, stopped, reopened)
    s = Stream(drone=None, ip='192.0.2.1')
    s._raw_frames = FakePublisher()

    with pytest.raises(StopReading):
        s._grab_frames()

    assert stopped.released and reopened.released
    assert len(s._raw_frames.published) == 2
    assert s._raw_frames.closed


def test_a_few_failed_reads_keep_the_stream(monkeypatch):
    reads = [FRAME] + [None] * (stream.MAX_FAILED_READS - 1) + [FRAME]
    flaky = FakeCapture(reads=reads)
    opening(monkeypatch, flaky)
    s = Stream(drone=None, ip='192.0.2.1')
    s._raw_frames = FakePublisher()

    with pytest.raises(StopReading):
        s._grab_frames()

    assert len(s._raw_frames.published) == 2
