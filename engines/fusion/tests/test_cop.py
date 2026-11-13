import logging

import pytest
import uriel
from conftest import LOCAL, REMOTE, Clock, input_id, make_event
from steeleagle_protocol.v1.messages.result import result_pb2

from fusion.cop import CopTracking, Mode, Verdict, check, iou
from fusion.detection import Report, Wait
from fusion.main import COP_TRACKING_PLUGIN, ConfigError, parse_args

K = uriel.EventKind
GRACE_MS = 100
TIMEOUT_MS = 1000


def detections(*items):
    """A serialized DetectionResult of the (class, score, box) `items`, each
    box (x_min, y_min, x_max, y_max)."""
    return result_pb2.DetectionResult(
        detections=[
            result_pb2.Detection(
                class_name=name,
                score=score,
                bbox=result_pb2.BoundingBox(
                    x_min=box[0], y_min=box[1], x_max=box[2], y_max=box[3]
                ),
            )
            for name, score, box in items
        ]
    ).SerializeToString()


BOX = (0.1, 0.1, 0.5, 0.5)
FAR = (0.6, 0.6, 0.9, 0.9)
# The person at BOX, with a decoy at FAR
GOOD = detections(("person", 0.9, BOX), ("car", 0.95, FAR))
# The person at FAR, more confident than at BOX
BAD = detections(("person", 0.4, BOX), ("person", 0.8, FAR))
NOTHING = detections(("car", 0.9, BOX))


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cop(clock):
    return CopTracking(
        local=LOCAL,
        remote=REMOTE,
        target="person",
        overlap_threshold=0.5,
        grace_ns=GRACE_MS * 1_000_000,
        timeout_ns=TIMEOUT_MS * 1_000_000,
        clock=clock,
    )


def result(cop, engine, seq, data, outcome=uriel.Outcome.FORWARD):
    cop.on_event(make_event(K.RESULT, engine, seq, data, outcome=outcome))


def decided(cop, seq):
    """The bytes the input is reported with, b"<drop>" if it is dropped with
    no result, or None if it waits."""
    decision = cop.decide(input_id(seq))
    if isinstance(decision, Wait):
        return None
    assert isinstance(decision, Report)
    return b"<drop>" if decision.result is None else decision.result.data


def run_input(cop, clock, seq, local, remote, remote_after_ms=10):
    """Begins input `seq`, delivers the local result, decides it if it can,
    then delivers the remote result `remote_after_ms` later, and decides it
    if it waited. Returns what it was reported with."""
    cop.begin(input_id(seq))
    result(cop, LOCAL, seq, local)
    reported = decided(cop, seq)
    clock.advance_ms(remote_after_ms)
    result(cop, REMOTE, seq, remote)
    if reported is None:
        reported = decided(cop, seq)
    cop.end(input_id(seq))
    return reported


def to_local_checked(cop, clock):
    assert run_input(cop, clock, 1, GOOD, GOOD) == GOOD
    assert cop.mode == Mode.LOCAL_CHECKED


def to_remote_only(cop, clock):
    to_local_checked(cop, clock)
    assert run_input(cop, clock, 2, BAD, GOOD) == BAD
    assert cop.mode == Mode.REMOTE_ONLY


# Checks


def test_iou():
    assert iou(BOX, BOX) == pytest.approx(1.0)
    assert iou(BOX, FAR) == 0.0
    assert iou((0, 0, 2, 1), (1, 0, 3, 1)) == pytest.approx(1 / 3)


def test_check_compares_the_most_confident_detection_of_the_class():
    assert check(GOOD, GOOD, "person", 0.5).verdict == Verdict.AGREE
    assert check(BAD, GOOD, "person", 0.5).verdict == Verdict.DISAGREE
    assert check(GOOD, None, "person", 0.5).verdict == Verdict.DISAGREE
    assert check(NOTHING, GOOD, "person", 0.5).verdict == Verdict.DISAGREE
    assert check(NOTHING, None, "person", 0.5).verdict == Verdict.INCONCLUSIVE
    assert check(b"\xff", GOOD, "person", 0.5).verdict == Verdict.INCONCLUSIVE


def test_class_is_case_insensitive(clock):
    cop = CopTracking(
        local=LOCAL,
        remote=REMOTE,
        target="Person",
        overlap_threshold=0.5,
        grace_ns=0,
        timeout_ns=1,
        clock=clock,
    )
    assert cop.target == "person"


@pytest.mark.parametrize("threshold", [0, -0.1, 1.5])
def test_threshold_must_be_in_range(clock, threshold):
    with pytest.raises(ValueError):
        CopTracking(
            local=LOCAL,
            remote=REMOTE,
            target="person",
            overlap_threshold=threshold,
            grace_ns=0,
            timeout_ns=1,
            clock=clock,
        )


# Modes


def test_starts_local_only_and_reports_local_at_once(cop, clock):
    assert cop.mode == Mode.LOCAL_ONLY
    cop.begin(input_id(1))
    result(cop, LOCAL, 1, BAD)
    assert decided(cop, 1) == BAD


def test_a_remote_result_in_time_goes_local_checked(cop, clock):
    to_local_checked(cop, clock)


def test_local_checked_reports_local_without_waiting(cop, clock):
    to_local_checked(cop, clock)
    cop.begin(input_id(2))
    result(cop, LOCAL, 2, GOOD)
    assert decided(cop, 2) == GOOD


def test_disagreement_goes_remote_only(cop, clock):
    to_remote_only(cop, clock)


def test_remote_only_waits_for_remote_and_reports_it(cop, clock):
    to_remote_only(cop, clock)
    cop.begin(input_id(3))
    result(cop, LOCAL, 3, BAD)
    assert cop.decide(input_id(3)) == Wait(clock.ns + GRACE_MS * 1_000_000)
    clock.advance_ms(GRACE_MS)
    result(cop, REMOTE, 3, GOOD)
    assert decided(cop, 3) == GOOD
    assert cop.mode == Mode.REMOTE_ONLY


def test_remote_only_reports_remote_before_local(cop, clock):
    to_remote_only(cop, clock)
    cop.begin(input_id(3))
    result(cop, REMOTE, 3, GOOD)
    assert decided(cop, 3) == GOOD


def test_agreement_returns_to_local_checked(cop, clock):
    to_remote_only(cop, clock)
    assert run_input(cop, clock, 3, GOOD, GOOD) == GOOD
    assert cop.mode == Mode.LOCAL_CHECKED


def test_agreement_after_reporting_remote_first(cop, clock):
    to_remote_only(cop, clock)
    cop.begin(input_id(3))
    result(cop, REMOTE, 3, GOOD)
    assert decided(cop, 3) == GOOD
    cop.end(input_id(3))
    clock.advance_ms(5)
    result(cop, LOCAL, 3, GOOD)
    assert cop.mode == Mode.LOCAL_CHECKED


def test_inconclusive_check_keeps_the_mode(cop, clock):
    to_remote_only(cop, clock)
    assert run_input(cop, clock, 3, NOTHING, NOTHING) == NOTHING
    assert cop.mode == Mode.REMOTE_ONLY


def test_a_late_remote_goes_local_only(cop, clock):
    to_local_checked(cop, clock)
    run_input(cop, clock, 2, BAD, GOOD, remote_after_ms=GRACE_MS + 1)
    # Late results are not checked
    assert cop.mode == Mode.LOCAL_ONLY


def test_remote_only_falls_back_to_local_when_grace_ends(cop, clock):
    to_remote_only(cop, clock)
    cop.begin(input_id(3))
    result(cop, LOCAL, 3, BAD)
    clock.advance_ms(GRACE_MS - 1)
    assert decided(cop, 3) is None
    clock.advance_ms(1)
    assert decided(cop, 3) == BAD
    assert cop.mode == Mode.LOCAL_ONLY


def test_remote_disconnect_goes_local_only(cop, clock):
    to_remote_only(cop, clock)
    cop.on_event(make_event(K.DISCONNECTED, REMOTE))
    assert cop.mode == Mode.LOCAL_ONLY
    cop.begin(input_id(3))
    result(cop, LOCAL, 3, BAD)
    assert decided(cop, 3) == BAD


def test_remote_timeout_goes_local_only(cop, clock):
    to_local_checked(cop, clock)
    cop.on_event(make_event(K.TIMED_OUT, REMOTE, 2))
    assert cop.mode == Mode.LOCAL_ONLY


def test_remote_skipping_is_not_unreachable(cop, clock):
    to_remote_only(cop, clock)
    cop.begin(input_id(3))
    result(cop, LOCAL, 3, BAD)
    cop.on_event(make_event(K.SKIPPED, REMOTE, 3))
    # Remote had nothing for this input, so fall back to local
    assert decided(cop, 3) == BAD
    assert cop.mode == Mode.REMOTE_ONLY


def test_remote_skipping_waits_for_local(cop, clock):
    to_remote_only(cop, clock)
    cop.begin(input_id(3))
    cop.on_event(make_event(K.SKIPPED, REMOTE, 3))
    assert decided(cop, 3) is None
    result(cop, LOCAL, 3, BAD)
    assert decided(cop, 3) == BAD
    assert cop.mode == Mode.REMOTE_ONLY


def test_back_from_local_only_through_local_checked(cop, clock):
    to_remote_only(cop, clock)
    cop.on_event(make_event(K.DISCONNECTED, REMOTE))
    # Remote comes back, still disagreeing: local-checked, then remote-only
    assert run_input(cop, clock, 3, BAD, GOOD) == BAD
    assert cop.mode == Mode.REMOTE_ONLY


def test_checks_inputs_the_engine_never_got(cop, clock):
    to_local_checked(cop, clock)
    result(cop, LOCAL, 2, BAD)
    result(cop, REMOTE, 2, GOOD)
    assert cop.mode == Mode.REMOTE_ONLY


def test_an_older_input_does_not_undo_a_newer_switch(cop, clock):
    to_local_checked(cop, clock)
    result(cop, LOCAL, 3, BAD)
    result(cop, REMOTE, 3, GOOD)
    assert cop.mode == Mode.REMOTE_ONLY
    result(cop, LOCAL, 2, GOOD)
    result(cop, REMOTE, 2, GOOD)
    assert cop.mode == Mode.REMOTE_ONLY


def test_local_no_result_is_reported_as_no_result_and_checked(cop, clock):
    to_local_checked(cop, clock)
    cop.begin(input_id(2))
    result(cop, LOCAL, 2, b"", outcome=uriel.Outcome.DROP)
    assert decided(cop, 2) == b"<drop>"
    cop.end(input_id(2))
    result(cop, REMOTE, 2, GOOD)
    assert cop.mode == Mode.REMOTE_ONLY


def test_reports_remote_if_local_fails(cop, clock):
    to_local_checked(cop, clock)
    cop.begin(input_id(2))
    cop.on_event(make_event(K.SKIPPED, LOCAL, 2))
    assert decided(cop, 2) is None
    result(cop, REMOTE, 2, GOOD)
    assert decided(cop, 2) == GOOD


def test_times_out_with_nothing(cop, clock):
    cop.begin(input_id(1))
    clock.advance_ms(TIMEOUT_MS)
    assert decided(cop, 1) == b"<drop>"


def test_finished_inputs_are_forgotten(cop, clock):
    to_local_checked(cop, clock)
    assert cop.tracked == 0


def test_logs_switches(cop, clock, caplog):
    caplog.set_level(logging.INFO, "fusion.cop")
    to_remote_only(cop, clock)
    switches = [r.getMessage() for r in caplog.records if " -> " in r.getMessage()]
    assert switches == [
        "input 1: local-only -> local-checked: remote answered in time",
        "input 2: local-checked -> remote-only:"
        " local and remote disagree, IoU 0.00 < 0.50",
    ]


# Args


def test_args():
    args = parse_args(
        COP_TRACKING_PLUGIN, '{"local": "a", "remote": "b", "class": "person"}'
    )
    assert args == {
        "local": "a",
        "remote": "b",
        "class": "person",
        "overlap_threshold": 0.5,
        "grace": 200,
        "timeout": 1000,
    }
    with pytest.raises(ConfigError):
        parse_args(COP_TRACKING_PLUGIN, '{"local": "a", "remote": "b"}')
    for bad in ("0", "1.5", "true"):
        with pytest.raises(ConfigError):
            parse_args(
                COP_TRACKING_PLUGIN,
                '{"local": "a", "remote": "b", "class": "x", "overlap_threshold": %s}'
                % bad,
            )


def test_remote_only_stops_waiting_when_remote_disconnects(cop, clock):
    to_remote_only(cop, clock)
    cop.begin(input_id(3))
    result(cop, LOCAL, 3, BAD)
    assert decided(cop, 3) is None
    clock.advance_ms(1)
    cop.on_event(make_event(K.DISCONNECTED, REMOTE))
    # Well before the grace period ends
    assert decided(cop, 3) == BAD
    assert cop.mode == Mode.LOCAL_ONLY


def test_remote_only_stops_waiting_when_remote_times_out(cop, clock):
    to_remote_only(cop, clock)
    cop.begin(input_id(3))
    result(cop, LOCAL, 3, BAD)
    assert decided(cop, 3) is None
    cop.on_event(make_event(K.TIMED_OUT, REMOTE, 3))
    assert decided(cop, 3) == BAD
    assert cop.mode == Mode.LOCAL_ONLY


def test_remote_only_disconnect_before_local_reports_local_on_arrival(cop, clock):
    to_remote_only(cop, clock)
    cop.begin(input_id(3))
    cop.on_event(make_event(K.DISCONNECTED, REMOTE))
    assert decided(cop, 3) is None
    result(cop, LOCAL, 3, BAD)
    assert decided(cop, 3) == BAD


def test_remote_only_falls_back_to_local_on_lost_events(cop, clock):
    to_remote_only(cop, clock)
    cop.begin(input_id(3))
    result(cop, LOCAL, 3, BAD)
    assert decided(cop, 3) is None
    cop.on_event(make_event(K.LOST, REMOTE))
    assert decided(cop, 3) == BAD
    # Lost events do not make remote unreachable
    assert cop.mode == Mode.REMOTE_ONLY


def test_remote_only_lost_events_before_local_waits_for_local(cop, clock):
    to_remote_only(cop, clock)
    cop.begin(input_id(3))
    cop.on_event(make_event(K.LOST, REMOTE))
    assert decided(cop, 3) is None
    result(cop, LOCAL, 3, BAD)
    assert decided(cop, 3) == BAD
