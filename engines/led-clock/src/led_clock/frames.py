"""A CSV file of one row per frame the engine received: when it got here, what
was read off the LED clock, and the latencies that follow."""

import csv
import logging

logger = logging.getLogger("led_clock.frames")

FIELDS = (
    "seq",
    "recv_ns",
    "fermi_ts_ns",
    "tick",
    "display_ns",
    "latency_ms",
    "fermi_latency_ms",
    "confidence",
    "step_ok",
)
_INTS = {"seq", "recv_ns", "fermi_ts_ns", "tick", "display_ns"}
_FLOATS = {"latency_ms", "fermi_latency_ms", "confidence"}
_BOOLS = {"step_ok"}


class CsvWriter:
    """Appends rows of FIELDS to the CSV file at `path`, writing the header
    only if the file is new or empty, so an engine restart keeps the earlier
    rows. An unknown value (None) is written as an empty field."""

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists() or path.stat().st_size == 0
        self._file = open(path, "a", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=FIELDS)
        if new:
            self._writer.writeheader()
        logger.info("writing a row per frame to %s", path)

    def write(self, row):
        self._writer.writerow(row)

    def flush(self):
        self._file.flush()

    def close(self):
        self._file.close()


def _parse(name, text):
    if text == "":
        return None
    if name in _INTS:
        return int(text)
    if name in _FLOATS:
        return float(text)
    if name in _BOOLS:
        return text == "True"
    return text


def read_rows(path):
    """The rows of the CSV file at `path`, with their values typed."""
    with open(path, newline="") as file:
        return [{name: _parse(name, row[name]) for name in FIELDS} for row in csv.DictReader(file)]
