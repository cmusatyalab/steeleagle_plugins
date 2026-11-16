import pytest

from led_clock.frames import FIELDS, CsvWriter
from led_clock.summarize import main, summarize

NANOS_PER_SECOND = 1_000_000_000


def row(seconds, latency_ms, confidence=1.0, fermi_latency_ms=None):
    return {name: None for name in FIELDS} | {
        "seq": int(seconds * 10),
        "recv_ns": int(seconds * NANOS_PER_SECOND),
        "latency_ms": latency_ms,
        "fermi_latency_ms": fermi_latency_ms,
        "confidence": confidence,
    }


ROWS = [
    row(0.0, 400.0, fermi_latency_ms=100.0),
    row(1.0, 500.0, fermi_latency_ms=200.0),
    row(2.0, 600.0, confidence=0.1),
    row(3.0, None, confidence=None),
    row(4.0, 900.0),
]


def test_the_stats_cover_the_decoded_frames():
    summary = summarize(ROWS)
    assert summary.frames == 5
    assert summary.decoded == 4
    latency = summary.latency_ms
    assert latency["count"] == 4
    assert latency["mean"] == pytest.approx(600.0)
    assert (latency["min"], latency["p50"], latency["max"]) == (400.0, 550.0, 900.0)
    assert summary.fermi_latency_ms["mean"] == pytest.approx(150.0)


def test_low_confidence_frames_are_left_out():
    latency = summarize(ROWS, min_confidence=0.5).latency_ms
    assert latency["count"] == 3
    assert latency["max"] == 900.0


def test_frames_outside_the_window_are_left_out():
    # Seconds from the first frame
    summary = summarize(ROWS, start_s=1.0, end_s=3.0)
    assert summary.frames == 3
    assert summary.latency_ms["count"] == 2


def test_the_command_prints_the_stats(tmp_path, capsys):
    path = tmp_path / "frames.csv"
    writer = CsvWriter(path)
    for r in ROWS:
        writer.write(r)
    writer.close()
    main([str(path), "--min-confidence", "0.5"])
    out = capsys.readouterr().out
    assert "3 of 5 frames used" in out
    assert "p99" in out and "mean" in out
