import copy
import functools

import numpy as np
import pytest
from conftest import Clock, FakeSaver, make_frame, make_image, to_gray

from led_clock.decode import BITS, LedReader
from led_clock.latency import LatencyLog, step_ok

TICK_NS = 5_000_000
FPS = 30
FRAME_TICKS = 1e9 / FPS / TICK_NS
FRAME_NS = 1_000_000_000 // FPS
REPORT_INTERVAL_NS = 5_000_000_000
SNAPSHOT_INTERVAL_NS = 2_000_000_000
# Glass to lambda2, and glass to fermi's publishing, of the frames sent
LATENCY_NS = 120_000_000
FERMI_LATENCY_NS = 70_000_000
START_TICK = 2_000_000
# The display time is the middle of the tick a frame was taken in, so a
# latency is up to half a tick off
HALF_TICK_MS = TICK_NS / 2 / 1e6


# Ticks between the frames that prime a reader: odd, so that every bit is seen
# both on and off, about as often as the running clock shows it
PRIME_STEP_TICKS = 7


@functools.cache
def _primed_reader():
    reader = LedReader()
    for tick in range(0, 1 << BITS, PRIME_STEP_TICKS):
        reader.read(make_image(to_gray(tick)))
    return reader


def primed_reader():
    """A reader that has seen the clock go through a whole wrap."""
    return copy.deepcopy(_primed_reader())


def make_log(clock, saver, reader=None):
    return LatencyLog(
        tick_ns=TICK_NS,
        fps=FPS,
        reader=reader if reader is not None else primed_reader(),
        saver=saver,
        clock=clock,
        report_interval_ns=REPORT_INTERVAL_NS,
        snapshot_interval_ns=SNAPSHOT_INTERVAL_NS,
    )


def tick_frame(tick, **kwargs):
    return make_frame(make_image(to_gray(tick % (1 << BITS))), **kwargs)


def send_frames(log, clock, count, *, first_seq=0):
    """Sends `count` frames, 30 fps apart, each received LATENCY_NS after the
    clock showed it and published by fermi FERMI_LATENCY_NS after."""
    for i in range(count):
        glass_ns = START_TICK * TICK_NS + i * FRAME_NS
        frame = tick_frame(glass_ns // TICK_NS, timestamp_ns=glass_ns + FERMI_LATENCY_NS)
        clock.ns += FRAME_NS
        log.on_frame(first_seq + i, frame, recv_ns=glass_ns + LATENCY_NS)


def end_interval(clock):
    clock.ns += REPORT_INTERVAL_NS


@pytest.mark.parametrize("step", [6, 7, 13, 14, 20])
def test_steps_of_whole_frames_are_ok(step):
    assert step_ok(step, FRAME_TICKS)


@pytest.mark.parametrize("step", [0, 1, 3, 9, 10])
def test_other_steps_are_not_ok(step):
    assert not step_ok(step, FRAME_TICKS)


def test_the_report_waits_for_its_interval():
    clock = Clock()
    log = make_log(clock, FakeSaver())
    send_frames(log, clock, 1)
    assert log.report() is None


def test_the_report_gives_the_latencies_seen():
    clock = Clock()
    log = make_log(clock, FakeSaver())
    send_frames(log, clock, 60)
    end_interval(clock)
    report = log.report()
    assert report.latency_ms[50] == pytest.approx(LATENCY_NS / 1e6, abs=HALF_TICK_MS)
    assert report.fermi_latency_ms[50] == pytest.approx(FERMI_LATENCY_NS / 1e6, abs=HALF_TICK_MS)
    assert (report.frames, report.decoded) == (60, 60)
    assert report.steps_ok == report.steps_checked == 59


def test_a_frame_without_the_clock_counts_as_not_decoded():
    clock = Clock()
    log = make_log(clock, FakeSaver())
    log.on_frame(0, make_frame(np.zeros_like(make_image(0))), recv_ns=0)
    end_interval(clock)
    report = log.report()
    assert (report.frames, report.decoded) == (1, 0)


def test_an_unreadable_input_counts_as_not_decoded():
    clock = Clock()
    log = make_log(clock, FakeSaver())
    log.on_frame(0, None, recv_ns=0)
    end_interval(clock)
    report = log.report()
    assert (report.frames, report.decoded) == (1, 0)


def test_a_bad_step_is_counted():
    clock = Clock()
    log = make_log(clock, FakeSaver())
    send_frames(log, clock, 2)
    # 3 ticks past the last frame's: not a whole number of frames
    last_tick = (START_TICK * TICK_NS + FRAME_NS) // TICK_NS
    log.on_frame(2, tick_frame(last_tick + 3), recv_ns=(last_tick + 3) * TICK_NS + LATENCY_NS)
    end_interval(clock)
    report = log.report()
    assert (report.steps_ok, report.steps_checked) == (1, 2)


def test_the_report_starts_over_each_interval():
    clock = Clock()
    log = make_log(clock, FakeSaver())
    send_frames(log, clock, 10)
    end_interval(clock)
    log.report()
    end_interval(clock)
    report = log.report()
    assert report.frames == 0
    assert report.latency_ms is None


def test_a_snapshot_is_saved_at_first_and_then_each_interval():
    clock = Clock()
    saver = FakeSaver()
    log = make_log(clock, saver)
    # 90 frames at 30 fps, 3 s: one snapshot at the first frame, the next 2 s on
    send_frames(log, clock, 90)
    first_due_after = SNAPSHOT_INTERVAL_NS // FRAME_NS + 1
    assert [name for name, _ in saver.saved] == ["0.jpeg", f"{first_due_after}.jpeg"]


def test_a_snapshot_has_the_reading_drawn_on_it():
    clock = Clock()
    saver = FakeSaver()
    log = make_log(clock, saver)
    raw = make_image(to_gray(123))
    log.on_frame(0, make_frame(raw), recv_ns=0)
    ((_, image),) = saver.saved
    assert image.shape == raw.shape
    assert not np.array_equal(image, raw)
