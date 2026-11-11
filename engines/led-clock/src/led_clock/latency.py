"""Turns what is read off the LED clock into latencies, and reports them.

For each frame, the code read is turned into when the clock showed it, and
two latencies are taken from that time:

- to here: the time the frame was received, less the display time;
- to fermi: the frame's timestamp, set when the driver on fermi published it,
  less the display time.

Both assume the clocks of the Pi showing the LEDs, fermi and this host agree.

As a check on the reading that needs no clocks, each code is compared with the
last one read: frames come 1/fps apart, so codes should step by about a whole
number of frames' worth of ticks (6.67 at 30 fps and 5 ms ticks).
"""

import logging
from dataclasses import dataclass, field

import numpy as np

from led_clock.decode import BITS, display_time_ns
from led_clock.snapshot import annotate

logger = logging.getLogger("led_clock")

NANOS_PER_MILLI = 1_000_000
NANOS_PER_SECOND = 1_000_000_000
PERCENTILES = (50, 95, 99, 100)
# How far a step may be from a whole number of frames, in ticks: each code is
# up to a tick off from when its frame was taken, from rounding and from an
# exposure that spans a tick change
STEP_TOLERANCE_TICKS = 2.0


def step_ok(step, frame_ticks, tolerance=STEP_TOLERANCE_TICKS):
    """Whether `step` ticks between two frames' codes is a whole number (at
    least one) of frames of `frame_ticks` ticks, within `tolerance`."""
    frames = round(step / frame_ticks)
    return frames >= 1 and abs(step - frames * frame_ticks) <= tolerance


def percentiles(samples):
    """{percentile: value} of `samples`, or None if there are none."""
    if not samples:
        return None
    return dict(zip(PERCENTILES, np.percentile(samples, PERCENTILES).tolist()))


@dataclass
class Report:
    seconds: float
    frames: int = 0
    decoded: int = 0
    steps_checked: int = 0
    steps_ok: int = 0
    # {percentile: milliseconds}, or None if no frame was decoded
    latency_ms: dict | None = None
    fermi_latency_ms: dict | None = None


@dataclass
class _Window:
    frames: int = 0
    decoded: int = 0
    steps_checked: int = 0
    steps_ok: int = 0
    latency_ms: list = field(default_factory=list)
    fermi_latency_ms: list = field(default_factory=list)


class LatencyLog:
    def __init__(
        self,
        *,
        tick_ns,
        fps,
        reader,
        saver,
        clock,
        report_interval_ns,
        snapshot_interval_ns,
    ):
        self.tick_ns = tick_ns
        self.frame_ticks = NANOS_PER_SECOND / fps / tick_ns
        self.reader = reader
        self.saver = saver
        self.clock = clock
        self.report_interval_ns = report_interval_ns
        self.snapshot_interval_ns = snapshot_interval_ns
        self._window = _Window()
        self._window_started_ns = clock()
        self._snapshot_due_ns = None
        self._last_code = None

    def on_frame(self, seq, frame, recv_ns):
        """Takes the RawFrame `frame` of input `seq` (None if it could not be
        parsed), received at `recv_ns` on the realtime clock."""
        window = self._window
        window.frames += 1
        if frame is None:
            return
        rgb = np.frombuffer(frame.data, dtype=np.uint8).reshape(frame.v_res, frame.h_res, -1)
        reading = self.reader.read(rgb)
        latency_ms = fermi_latency_ms = None
        if reading.code is not None:
            window.decoded += 1
            if self._last_code is not None:
                window.steps_checked += 1
                step = (reading.code - self._last_code) % (1 << BITS)
                window.steps_ok += step_ok(step, self.frame_ticks)
            self._last_code = reading.code
            display_ns = display_time_ns(reading.code, recv_ns, self.tick_ns)
            latency_ms = (recv_ns - display_ns) / NANOS_PER_MILLI
            window.latency_ms.append(latency_ms)
            if frame.HasField("timestamp"):
                fermi_latency_ms = (frame.timestamp.ToNanoseconds() - display_ns) / NANOS_PER_MILLI
                window.fermi_latency_ms.append(fermi_latency_ms)
        self._snapshot(seq, rgb, reading, latency_ms, fermi_latency_ms)

    def report(self):
        """Logs and returns the Report of the frames since the last, if one is
        due; otherwise returns None."""
        now = self.clock()
        if now - self._window_started_ns < self.report_interval_ns:
            return None
        window = self._window
        report = Report(
            seconds=(now - self._window_started_ns) / NANOS_PER_SECOND,
            frames=window.frames,
            decoded=window.decoded,
            steps_checked=window.steps_checked,
            steps_ok=window.steps_ok,
            latency_ms=percentiles(window.latency_ms),
            fermi_latency_ms=percentiles(window.fermi_latency_ms),
        )
        logger.info(format_report(report))
        self._window = _Window()
        self._window_started_ns = now
        return report

    def _snapshot(self, seq, rgb, reading, latency_ms, fermi_latency_ms):
        now = self.clock()
        if self._snapshot_due_ns is not None and now < self._snapshot_due_ns:
            return
        self._snapshot_due_ns = now + self.snapshot_interval_ns
        if reading.code is None:
            lines = [f"input {seq}: not decoded"]
        else:
            lines = [
                # Bit 0 first, as the LEDs are left to right
                f"input {seq}: LEDs {''.join(str(int(b)) for b in reading.bits)}"
                f" = gray {reading.gray} -> tick {reading.code}"
                f" (confidence {reading.confidence:.2f})",
                f"latency {latency_ms:.1f} ms"
                + (f", to fermi {fermi_latency_ms:.1f} ms" if fermi_latency_ms is not None else ""),
            ]
        if reading.levels is not None:
            lines.append("levels " + " ".join(f"{level:.0f}" for level in reading.levels))
        if reading.thresholds is not None:
            lines.append("thresholds " + " ".join(f"{t:.0f}" for t in reading.thresholds))
        self.saver.save(f"{seq}.jpeg", annotate(rgb, reading, lines))


def _format_percentiles(name, values):
    if values is None:
        return f"{name}: none"
    return f"{name} ms: " + " ".join(
        f"{'max' if p == 100 else f'p{p}'} {v:.1f}" for p, v in values.items()
    )


def format_report(report):
    decoded_pct = 100 * report.decoded / report.frames if report.frames else 0.0
    return (
        f"last {report.seconds:.0f} s: {report.frames} frames, {report.decoded} decoded"
        f" ({decoded_pct:.0f}%), steps ok {report.steps_ok}/{report.steps_checked}"
        f" | {_format_percentiles('latency', report.latency_ms)}"
        f" | {_format_percentiles('to fermi', report.fermi_latency_ms)}"
    )
