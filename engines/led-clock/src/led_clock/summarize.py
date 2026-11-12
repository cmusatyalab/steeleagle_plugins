"""Prints latency statistics of a run from the engine's frames.csv.

    pixi run summarize <frames.csv> [--min-confidence C] [--start S] [--end S]
"""

import argparse
from dataclasses import dataclass

from led_clock.frames import read_rows
from led_clock.stats import format_stats, latency_stats

NANOS_PER_SECOND = 1_000_000_000


@dataclass
class Summary:
    frames: int
    decoded: int
    used: int
    latency_ms: dict | None
    fermi_latency_ms: dict | None


def summarize(rows, *, min_confidence=None, start_s=None, end_s=None):
    """The Summary of `rows` (from read_rows). Only frames received between
    `start_s` and `end_s` seconds after the first are counted, and only
    decoded ones with at least `min_confidence` give latencies."""
    if rows:
        first_ns = min(row["recv_ns"] for row in rows)
        seconds = lambda row: (row["recv_ns"] - first_ns) / NANOS_PER_SECOND
        rows = [
            row
            for row in rows
            if (start_s is None or seconds(row) >= start_s)
            and (end_s is None or seconds(row) <= end_s)
        ]
    decoded = [row for row in rows if row["latency_ms"] is not None]
    used = [
        row
        for row in decoded
        if min_confidence is None or (row["confidence"] or 0.0) >= min_confidence
    ]
    return Summary(
        frames=len(rows),
        decoded=len(decoded),
        used=len(used),
        latency_ms=latency_stats([row["latency_ms"] for row in used]),
        fermi_latency_ms=latency_stats(
            [row["fermi_latency_ms"] for row in used if row["fermi_latency_ms"] is not None]
        ),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Latency statistics of an LED clock run")
    parser.add_argument("csv", help="frames.csv written by the led-clock engine")
    parser.add_argument("--min-confidence", type=float, default=None,
                        help="leave out frames decoded with less confidence than this (0 to 1)")
    parser.add_argument("--start", type=float, default=None,
                        help="leave out frames received sooner than this many seconds after the first")
    parser.add_argument("--end", type=float, default=None,
                        help="leave out frames received later than this many seconds after the first")
    args = parser.parse_args(argv)
    summary = summarize(
        read_rows(args.csv), min_confidence=args.min_confidence, start_s=args.start, end_s=args.end
    )
    print(f"{summary.used} of {summary.frames} frames used ({summary.decoded} decoded)")
    print(format_stats("latency", summary.latency_ms))
    print(format_stats("to fermi", summary.fermi_latency_ms))


if __name__ == "__main__":
    main()
