import pytest

from window import NANOS_PER_MILLI, NANOS_PER_SECOND, DropCounter, Window

FRAME_BYTES = 1000
LATENCY_MS = (10, 20, 30, 40, 50)


def test_consecutive_ids_lose_nothing():
    drops = DropCounter()
    assert [drops.observe(i) for i in range(5)] == [0] * 5
    assert drops.lost == 0


def test_gap_counts_missing_frames():
    drops = DropCounter()
    drops.observe(10)
    assert drops.observe(13) == 2
    assert drops.lost == 2


def test_first_frame_may_start_anywhere():
    assert DropCounter().observe(500) == 0


def test_backwards_id_is_a_restart():
    drops = DropCounter()
    drops.observe(7)
    assert drops.observe(0) == 0
    assert drops.observe(1) == 0
    assert (drops.lost, drops.restarts) == (0, 1)


def test_empty_window_has_no_summary():
    assert Window().summary(NANOS_PER_SECOND) is None


def test_window_summary():
    window = Window()
    for latency_ms in LATENCY_MS:
        window.add(latency_ms * NANOS_PER_MILLI, FRAME_BYTES, 0)
    window.lost = 5
    summary = window.summary(NANOS_PER_SECOND)
    assert summary["frames"] == 5
    assert summary["loss_pct"] == pytest.approx(50)
    assert summary["fps"] == pytest.approx(5)
    assert summary["mbps"] == pytest.approx(5 * FRAME_BYTES * 8 / 1e6)
    assert (summary["min_ms"], summary["p50_ms"], summary["max_ms"]) == pytest.approx((10, 30, 50))
    assert summary["mean_ms"] == pytest.approx(30)
