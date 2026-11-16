import uriel
from conftest import (
    DETECTOR,
    Clock,
    FakeSaver,
    input_id,
    make_event,
    make_frame,
    make_result,
)

from detection_log.log import MAX_WAITING, DetectionLog

TIMEOUT_MS = 1000
RESULT = uriel.EventKind.RESULT


def make_log(saver=None):
    clock = Clock()
    log = DetectionLog(
        detector=DETECTOR,
        saver=saver or FakeSaver(),
        timeout_ns=TIMEOUT_MS * 1_000_000,
        clock=clock,
    )
    return log, clock


def result_event(seq, *boxes, **kwargs):
    return make_event(RESULT, seq, make_result(*boxes).SerializeToString(), **kwargs)


def saved_seqs(log):
    return [seq for seq, _, _ in log.saver.saved]


def test_a_frame_is_saved_with_the_result_that_comes_after_it():
    log, _ = make_log()
    frame = make_frame()
    log.on_frame(input_id(3), frame)
    assert log.saver.saved == []
    log.on_event(result_event(3, (0.1, 0.2, 0.3, 0.4)))
    ((seq, saved, result),) = log.saver.saved
    assert seq == 3 and saved is frame
    assert result.detections[0].bbox.x_min == 0.2
    assert log.waiting == 0


def test_a_frame_is_saved_at_once_with_the_result_that_came_before_it():
    log, _ = make_log()
    log.on_event(result_event(3, (0.1, 0.2, 0.3, 0.4)))
    log.on_frame(input_id(3), make_frame())
    assert saved_seqs(log) == [3]
    assert log.waiting == 0


def test_a_frame_the_detector_dropped_with_no_result_is_saved_with_no_boxes():
    log, _ = make_log()
    log.on_frame(input_id(1), make_frame())
    log.on_event(make_event(RESULT, 1, outcome=uriel.Outcome.DROP))
    ((_, _, result),) = log.saver.saved
    assert len(result.detections) == 0


def test_a_result_written_with_dots_is_read():
    log, _ = make_log()
    log.on_frame(input_id(1), make_frame())
    log.on_event(
        result_event(
            1,
            (0.1, 0.1, 0.2, 0.2),
            payload_type="steeleagle_protocol.v1.messages.result.DetectionResult",
        )
    )
    assert len(log.saver.saved[0][2].detections) == 1


def test_frames_are_matched_to_their_own_results():
    log, _ = make_log()
    log.on_frame(input_id(1), make_frame())
    log.on_frame(input_id(2), make_frame())
    log.on_event(result_event(2, (0.5, 0.5, 0.6, 0.6)))
    log.on_event(result_event(1))
    assert saved_seqs(log) == [2, 1]
    assert len(log.saver.saved[0][2].detections) == 1
    assert len(log.saver.saved[1][2].detections) == 0


def test_events_of_other_engines_are_ignored():
    log, _ = make_log()
    log.on_frame(input_id(1), make_frame())
    log.on_event(result_event(1, engine="100.64.0.1.other"))
    assert log.saver.saved == []
    assert log.waiting == 1


def test_a_frame_the_detector_skipped_or_timed_out_on_is_not_saved():
    log, _ = make_log()
    log.on_frame(input_id(1), make_frame())
    log.on_event(make_event(uriel.EventKind.SKIPPED, 1))
    log.on_event(make_event(uriel.EventKind.TIMED_OUT, 2))
    log.on_frame(input_id(2), make_frame())
    assert log.saver.saved == []
    assert log.waiting == 0


def test_an_undecodable_result_is_not_saved():
    log, _ = make_log()
    log.on_frame(input_id(1), make_frame())
    log.on_event(make_event(RESULT, 1, b"\xff\xff\xff"))
    log.on_frame(input_id(2), make_frame())
    log.on_event(make_event(RESULT, 2, b"x", payload_type="some/OtherType"))
    assert log.saver.saved == []
    assert log.waiting == 0


def test_a_frame_whose_result_does_not_come_in_time_is_given_up():
    log, clock = make_log()
    log.on_frame(input_id(1), make_frame())
    clock.advance_ms(TIMEOUT_MS - 1)
    assert log.expire() == clock() + 1_000_000
    assert log.waiting == 1
    clock.advance_ms(1)
    assert log.expire() is None
    assert log.waiting == 0
    # A result after that is kept, but nothing is saved
    log.on_event(result_event(1))
    assert log.saver.saved == []


def test_a_disconnect_gives_up_on_waiting_frames_and_new_ones_until_heard_from():
    log, _ = make_log()
    log.on_frame(input_id(1), make_frame())
    log.on_event(make_event(uriel.EventKind.DISCONNECTED))
    assert log.waiting == 0
    log.on_frame(input_id(2), make_frame())
    assert log.waiting == 0
    log.on_event(result_event(2))
    log.on_frame(input_id(3), make_frame())
    log.on_event(result_event(3))
    assert saved_seqs(log) == [3]


def test_past_the_most_waiting_frames_the_oldest_is_given_up():
    log, _ = make_log()
    for seq in range(MAX_WAITING + 1):
        log.on_frame(input_id(seq), make_frame())
    assert log.waiting == MAX_WAITING
    log.on_event(result_event(0))
    log.on_event(result_event(1))
    assert saved_seqs(log) == [1]


def test_an_unreadable_frame_is_not_saved():
    log, _ = make_log()
    log.on_event(result_event(1))
    log.on_frame(input_id(1), None)
    assert log.saver.saved == []
    assert log.waiting == 0
