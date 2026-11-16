import csv

import pytest
import uriel
from conftest import DETECTOR, Clock, FakeSaver, input_id, make_event, make_frame, make_result

from detection_log.boxes import COLUMNS, BoxLog
from detection_log.log import DetectionLog

RESULT = uriel.EventKind.RESULT


class WallClock:
    def __init__(self, now=5_000):
        self.now = now

    def __call__(self):
        return self.now


def make_log(tmp_path, saver=None):
    wall = WallClock()
    log = DetectionLog(
        detector=DETECTOR,
        saver=saver,
        boxes=BoxLog(tmp_path / "logs" / "boxes.csv"),
        timeout_ns=10**9,
        clock=Clock(),
        wall_clock=wall,
    )
    return log, wall


def read(log):
    log.close()
    with open(log.boxes.path, newline="") as f:
        return list(csv.DictReader(f))


def frame(id, stamp_ns=None):
    f = make_frame(width=64, height=48)
    f.id = id
    if stamp_ns is not None:
        f.timestamp.seconds, f.timestamp.nanos = divmod(stamp_ns, 1_000_000_000)
    return f


def result_event(seq, *boxes):
    return make_event(RESULT, seq, make_result(*boxes).SerializeToString())


def test_each_box_is_a_row_with_the_frame_its_capture_and_when_the_result_came(tmp_path):
    log, wall = make_log(tmp_path)
    log.on_frame(input_id(3), frame(41, stamp_ns=2_000_000_123))
    wall.now = 7_000
    log.on_event(result_event(3, (0.1, 0.2, 0.3, 0.4), (0.5, 0.6, 0.7, 0.8)))
    rows = read(log)
    assert list(rows[0]) == list(COLUMNS)
    assert [r["y_min"] for r in rows] == ["0.10000", "0.50000"]
    first = rows[0]
    assert (first["frame_id"], first["timestamp_ns"], first["result_ns"], first["seq"]) == (
        "41", "2000000123", "7000", "3")
    assert (first["x_min"], first["x_max"], first["y_max"]) == ("0.20000", "0.40000", "0.30000")
    assert (first["class"], first["score"], first["frame_width"], first["frame_height"]) == (
        "person", "0.9000", "64", "48")


def test_the_result_time_is_when_it_arrived_even_before_the_frame(tmp_path):
    log, wall = make_log(tmp_path)
    wall.now = 6_000
    log.on_event(result_event(1, (0.1, 0.2, 0.3, 0.4)))
    wall.now = 9_000
    log.on_frame(input_id(1), frame(1))
    assert read(log)[0]["result_ns"] == "6000"


def test_a_frame_with_no_detections_is_one_empty_row(tmp_path):
    log, _ = make_log(tmp_path)
    log.on_frame(input_id(2), frame(8))
    log.on_event(make_event(RESULT, 2, outcome=uriel.Outcome.DROP))
    (row,) = read(log)
    assert row["frame_id"] == "8" and row["class"] == "" and row["x_min"] == ""
    assert row["timestamp_ns"] == ""  # the frame had no timestamp


def test_a_frame_the_detector_skipped_is_not_logged(tmp_path):
    log, _ = make_log(tmp_path)
    log.on_frame(input_id(2), frame(8))
    log.on_event(make_event(uriel.EventKind.SKIPPED, 2))
    assert read(log) == []


def test_frames_go_to_both_the_saver_and_the_box_log(tmp_path):
    log, _ = make_log(tmp_path, saver=FakeSaver())
    log.on_frame(input_id(2), frame(8))
    log.on_event(result_event(2, (0.1, 0.2, 0.3, 0.4)))
    assert [seq for seq, _, _ in log.saver.saved] == [2]
    assert len(read(log)) == 1


def test_a_log_with_nowhere_to_save_is_refused():
    with pytest.raises(ValueError):
        DetectionLog(detector=DETECTOR, timeout_ns=1, clock=Clock())
