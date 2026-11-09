"""Statistics of the frames a metrics engine receives. Pure: nothing here
reads a clock or touches Uriel, so it is tested on its own."""
from dataclasses import dataclass, field

import numpy as np

NANOS_PER_MILLI = 1_000_000
NANOS_PER_SECOND = 1_000_000_000
BITS_PER_BYTE = 8
BITS_PER_MEGABIT = 1_000_000
PERCENTILES = (50, 95, 99)


@dataclass
class DropCounter:
    """Counts frames lost from gaps in the ids of consecutive frames. An id
    below the next expected one is a duplicate, a reordering or a restarted
    sender; it is counted as a restart and ids resume from it."""
    expected: int | None = None
    lost: int = 0
    restarts: int = 0

    def observe(self, frame_id):
        """Returns how many frames went missing before `frame_id`."""
        missing = 0
        if self.expected is None or frame_id >= self.expected:
            if self.expected is not None:
                missing = frame_id - self.expected
        else:
            self.restarts += 1
        self.lost += missing
        self.expected = frame_id + 1
        return missing


@dataclass
class Window:
    """Frames received since the last report."""
    latencies_ns: list = field(default_factory=list)
    bytes: int = 0
    lost: int = 0

    def add(self, latency_ns, size, missing):
        self.latencies_ns.append(latency_ns)
        self.bytes += size
        self.lost += missing

    def summary(self, duration_ns):
        """A dict of the window's latency percentiles (ms), rates and drops;
        None if no frame arrived."""
        if not self.latencies_ns:
            return None
        latencies_ms = np.asarray(self.latencies_ns) / NANOS_PER_MILLI
        seconds = duration_ns / NANOS_PER_SECOND
        expected = len(latencies_ms) + self.lost
        summary = {
            "frames": len(latencies_ms),
            "lost": self.lost,
            "loss_pct": 100 * self.lost / expected,
            "fps": len(latencies_ms) / seconds,
            "mbps": self.bytes * BITS_PER_BYTE / BITS_PER_MEGABIT / seconds,
            "min_ms": float(latencies_ms.min()),
            "mean_ms": float(latencies_ms.mean()),
            "max_ms": float(latencies_ms.max()),
        }
        for percentile, value in zip(PERCENTILES, np.percentile(latencies_ms, PERCENTILES)):
            summary[f"p{percentile}_ms"] = float(value)
        return summary
