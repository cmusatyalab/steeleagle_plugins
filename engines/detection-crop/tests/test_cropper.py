import pytest
import uriel
from conftest import DETECTOR, VERIFIER, Clock, input_id, make_event, make_frame, make_result
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.result import result_pb2

from detection_crop.cropper import (
    CROPPED,
    DISCONNECTED,
    NO_DETECTION,
    SKIPPED,
    TIMED_OUT,
    TOO_BIG,
    TOO_LATE,
    UNDECODABLE,
    Cropper,
)
from detection_crop.send_queue import SendQueue

FRAME = make_frame(64, 48, color=None).SerializeToString()


def make_cropper(clock=None, max_output_size=1 << 20, sent_log=None):
    return Cropper(
        detector=DETECTOR,
        class_name="person",
        verifier=VERIFIER,
        queue=SendQueue(max_size=8, resend_after_s=5.0),
        timeout_ns=2_000_000_000,
        max_output_size=max_output_size,
        clock=clock or Clock(),
        sent_log=sent_log,
    )


def result_event(seq, *boxes, class_name="person", score=0.9):
    result = make_result(*boxes, class_name=class_name, score=score)
    return make_event(uriel.EventKind.RESULT, seq, result.SerializeToString())


def test_a_result_before_the_frame_crops_it_to_the_best_box():
    cropper = make_cropper()
    result = make_result((0, 0, 1, 1), score=0.3)
    result.MergeFrom(make_result((0.5, 0.5, 1.0, 1.0), score=0.9))
    cropper.on_event(make_event(uriel.EventKind.RESULT, 0, result.SerializeToString()))
    outcome = cropper.outcome(input_id(0), cropper.deadline())
    crop, result = cropper.apply(input_id(0), FRAME, outcome, now=0)
    out = frame_pb2.RawFrame.FromString(crop.payload)
    assert (out.h_res, out.v_res) == (32, 24)
    assert cropper._counts[CROPPED] == 1
    # The result classifies the crop: the detection's class and confidence
    classification = result_pb2.ClassificationResult.FromString(result)
    assert getattr(classification, "class") == "person"
    assert classification.score == pytest.approx(0.9)
    assert crop.score == pytest.approx(0.9)


def test_a_crop_is_queued_in_flight_until_the_verifier_has_it():
    cropper = make_cropper()
    cropper.on_event(result_event(0, (0, 0, 1, 1)))
    outcome = cropper.outcome(input_id(0), cropper.deadline())
    crop, _ = cropper.apply(input_id(0), FRAME, outcome, now=0)
    assert crop in cropper.queue and crop.carriers == [input_id(0)]
    # The detector's events are not the verifier's
    cropper.on_event(make_event(uriel.EventKind.SKIPPED, 0))
    assert crop.in_flight
    # The verifier timed out: the crop waits to be sent again
    cropper.on_event(make_event(uriel.EventKind.TIMED_OUT, 0, engine=VERIFIER))
    assert not crop.in_flight and cropper.queue.best(1) is crop
    # The verifier has it: gone
    cropper.queue.sent(crop, input_id(5), 1)
    cropper.on_event(make_event(uriel.EventKind.RESULT, 5, engine=VERIFIER))
    assert len(cropper.queue) == 0 and cropper.queue.confirmed == 1


def test_a_frame_waits_for_its_result_until_the_deadline():
    clock = Clock()
    cropper = make_cropper(clock)
    deadline = cropper.deadline()
    # Another input's result does not answer it
    cropper.on_event(result_event(1, (0, 0, 1, 1)))
    assert cropper.outcome(input_id(0), deadline) is None
    clock.advance_ms(1999)
    assert cropper.outcome(input_id(0), deadline) is None
    cropper.on_event(result_event(0, (0, 0, 0.5, 0.5)))
    assert len(cropper.outcome(input_id(0), deadline).detections) == 1


def test_a_frame_with_no_result_in_time_is_dropped():
    clock = Clock()
    cropper = make_cropper(clock)
    deadline = cropper.deadline()
    clock.advance_ms(2000)
    outcome = cropper.outcome(input_id(0), deadline)
    assert outcome == TOO_LATE
    assert cropper.apply(input_id(0), FRAME, outcome) == (None, b"")
    assert cropper._counts[TOO_LATE] == 1


def test_failures_drop_the_frame():
    cropper = make_cropper()
    cropper.on_event(make_event(uriel.EventKind.SKIPPED, 0))
    cropper.on_event(make_event(uriel.EventKind.TIMED_OUT, 1))
    cropper.on_event(make_event(uriel.EventKind.RESULT, 2, b"\xff\xff", payload_type="x/Y"))
    for seq, why in enumerate([SKIPPED, TIMED_OUT, UNDECODABLE]):
        outcome = cropper.outcome(input_id(seq), cropper.deadline())
        assert outcome == why
        assert cropper.apply(input_id(seq), FRAME, outcome) == (None, b"")


def test_a_result_with_nothing_of_the_class_drops_the_frame():
    cropper = make_cropper()
    cropper.on_event(result_event(0, (0, 0, 1, 1), class_name="car"))
    # A detector that drops the input with no result found nothing
    cropper.on_event(make_event(uriel.EventKind.RESULT, 1, outcome=uriel.Outcome.DROP))
    for seq in (0, 1):
        outcome = cropper.outcome(input_id(seq), cropper.deadline())
        assert cropper.apply(input_id(seq), FRAME, outcome) == (None, b"")
    assert cropper._counts[NO_DETECTION] == 2
    assert len(cropper.queue) == 0


def test_events_of_other_engines_are_ignored():
    cropper = make_cropper()
    cropper.on_event(
        make_event(
            uriel.EventKind.RESULT,
            0,
            make_result((0, 0, 1, 1)).SerializeToString(),
            engine="other",
        )
    )
    assert cropper.outcome(input_id(0), cropper.deadline()) is None


def test_a_disconnected_detector_fails_frames_until_heard_from():
    cropper = make_cropper()
    cropper.on_event(make_event(uriel.EventKind.DISCONNECTED))
    assert cropper.outcome(input_id(0), cropper.deadline()) == DISCONNECTED
    cropper.on_event(result_event(1, (0, 0, 1, 1)))
    assert cropper.outcome(input_id(2), cropper.deadline()) is None


def test_a_crop_over_the_output_size_is_dropped():
    cropper = make_cropper(max_output_size=1000)
    cropper.on_event(result_event(0, (0, 0, 1, 1)))
    outcome = cropper.outcome(input_id(0), cropper.deadline())
    assert cropper.apply(input_id(0), FRAME, outcome) == (None, b"")
    assert cropper._counts[TOO_BIG] == 1
    assert len(cropper.queue) == 0


def test_each_crop_is_logged_with_its_box_in_the_frame(tmp_path, monkeypatch):
    import csv

    from detection_crop.sent_log import SentLog

    monkeypatch.setenv("STEELEAGLE_FILES", str(tmp_path))
    log = SentLog("crops/sent.csv")
    cropper = make_cropper(sent_log=log)
    cropper.on_event(result_event(0, (0.5, 0.5, 1.0, 1.0), score=0.75))
    outcome = cropper.outcome(input_id(0), cropper.deadline())
    cropper.apply(input_id(0), FRAME, outcome, now=0)
    # Nothing of the class: nothing logged
    cropper.on_event(result_event(1, (0, 0, 1, 1), class_name="car"))
    cropper.apply(input_id(1), FRAME, cropper.outcome(input_id(1), cropper.deadline()), now=0)
    log.close()
    rows = list(csv.DictReader(open(tmp_path / "crops" / "sent.csv")))
    assert len(rows) == 1
    frame = frame_pb2.RawFrame.FromString(FRAME)
    row = rows[0]
    assert int(row["frame_id"]) == frame.id
    assert int(row["timestamp_ns"]) == frame.timestamp.ToNanoseconds()
    assert [int(row[k]) for k in ("left", "top", "right", "bottom")] == [32, 24, 64, 48]
    assert (int(row["frame_width"]), int(row["frame_height"])) == (64, 48)
    assert float(row["score"]) == pytest.approx(0.75)
    assert row["class"] == "person"
