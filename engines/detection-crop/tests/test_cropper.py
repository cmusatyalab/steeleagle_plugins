import uriel
from conftest import DETECTOR, Clock, input_id, make_event, make_frame, make_result
from steeleagle_protocol.v1.messages.frame import frame_pb2

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

FRAME = make_frame(64, 48, color=None).SerializeToString()


def make_cropper(clock=None, max_output_size=1 << 20):
    return Cropper(
        detector=DETECTOR,
        class_name="person",
        timeout_ns=2_000_000_000,
        max_output_size=max_output_size,
        clock=clock or Clock(),
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
    out = frame_pb2.RawFrame.FromString(cropper.apply(input_id(0), FRAME, outcome))
    assert (out.h_res, out.v_res) == (32, 24)
    assert cropper._counts[CROPPED] == 1


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
    assert cropper.apply(input_id(0), FRAME, outcome) is None
    assert cropper._counts[TOO_LATE] == 1


def test_failures_drop_the_frame():
    cropper = make_cropper()
    cropper.on_event(make_event(uriel.EventKind.SKIPPED, 0))
    cropper.on_event(make_event(uriel.EventKind.TIMED_OUT, 1))
    cropper.on_event(make_event(uriel.EventKind.RESULT, 2, b"\xff\xff", payload_type="x/Y"))
    for seq, why in enumerate([SKIPPED, TIMED_OUT, UNDECODABLE]):
        outcome = cropper.outcome(input_id(seq), cropper.deadline())
        assert outcome == why
        assert cropper.apply(input_id(seq), FRAME, outcome) is None


def test_a_result_with_nothing_of_the_class_drops_the_frame():
    cropper = make_cropper()
    cropper.on_event(result_event(0, (0, 0, 1, 1), class_name="car"))
    # A detector that drops the input with no result found nothing
    cropper.on_event(make_event(uriel.EventKind.RESULT, 1, outcome=uriel.Outcome.DROP))
    for seq in (0, 1):
        outcome = cropper.outcome(input_id(seq), cropper.deadline())
        assert cropper.apply(input_id(seq), FRAME, outcome) is None
    assert cropper._counts[NO_DETECTION] == 2


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
    assert cropper.apply(input_id(0), FRAME, outcome) is None
    assert cropper._counts[TOO_BIG] == 1
