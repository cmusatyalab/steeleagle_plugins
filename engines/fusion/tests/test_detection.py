import logging

import pytest
import uriel
from conftest import (
    DETECTION_RESULT_TYPE,
    LOCAL,
    PRODUCER,
    REMOTE,
    SESSION,
    Clock,
    input_id,
    make_event,
)

from fusion import detection
from fusion.detection import DetectionFusion, Report, Result, Wait
from fusion.engine import _report

K = uriel.EventKind
GRACE_MS = 100
TIMEOUT_MS = 1000


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fusion(clock):
    return DetectionFusion(
        local=LOCAL,
        remote=REMOTE,
        grace_ns=GRACE_MS * 1_000_000,
        timeout_ns=TIMEOUT_MS * 1_000_000,
        clock=clock,
    )


def result(fusion, engine, seq, data):
    fusion.on_event(make_event(K.RESULT, engine, seq, data))


def decided(fusion, seq):
    """The bytes the input is reported with, b"<drop>" if it is dropped with
    no result, or None if it waits."""
    decision = fusion.decide(input_id(seq))
    if isinstance(decision, Wait):
        return None
    return b"<drop>" if decision.result is None else decision.result.data


def test_engines_must_differ():
    with pytest.raises(ValueError):
        DetectionFusion(local="a", remote="a", grace_ns=0, timeout_ns=0, clock=Clock())


def test_waits_until_an_engine_reports(fusion, clock):
    fusion.begin(input_id(1))
    assert fusion.decide(input_id(1)) == Wait(clock.ns + TIMEOUT_MS * 1_000_000)
    fusion.on_event(make_event(K.ACCEPTED, LOCAL, 1))
    fusion.on_event(make_event(K.SENT, REMOTE, 1))
    assert decided(fusion, 1) is None


def test_remote_result_within_grace_wins(fusion, clock):
    fusion.begin(input_id(1))
    result(fusion, LOCAL, 1, b"local")
    assert fusion.decide(input_id(1)) == Wait(clock.ns + GRACE_MS * 1_000_000)
    clock.advance_ms(GRACE_MS)
    result(fusion, REMOTE, 1, b"remote")
    decision = fusion.decide(input_id(1))
    assert isinstance(decision, Report) and decision.source == REMOTE
    assert decision.result.data == b"remote"


def test_local_result_once_grace_expires(fusion, clock):
    fusion.begin(input_id(1))
    result(fusion, LOCAL, 1, b"local")
    clock.advance_ms(GRACE_MS - 1)
    assert decided(fusion, 1) is None
    clock.advance_ms(1)
    assert decided(fusion, 1) == b"local"


def test_remote_result_after_grace_loses(fusion, clock):
    # Both results were in before the input reached the engine
    result(fusion, LOCAL, 1, b"local")
    clock.advance_ms(GRACE_MS + 1)
    result(fusion, REMOTE, 1, b"remote")
    fusion.begin(input_id(1))
    assert decided(fusion, 1) == b"local"


def test_remote_result_before_local_wins_at_once(fusion):
    fusion.begin(input_id(1))
    result(fusion, REMOTE, 1, b"remote")
    assert decided(fusion, 1) == b"remote"


@pytest.mark.parametrize(
    "event",
    [
        make_event(K.SKIPPED, REMOTE, 1),
        make_event(K.TIMED_OUT, REMOTE, 1),
        make_event(K.RESULT, REMOTE, 1, outcome=uriel.Outcome.DROP),
        make_event(K.DISCONNECTED, REMOTE),
    ],
    ids=["skipped", "timed_out", "dropped", "disconnected"],
)
def test_local_result_at_once_if_remote_fails(fusion, event):
    fusion.begin(input_id(1))
    result(fusion, LOCAL, 1, b"local")
    assert decided(fusion, 1) is None
    fusion.on_event(event)
    assert decided(fusion, 1) == b"local"


def test_a_dropped_input_with_a_result_is_a_result(fusion):
    fusion.begin(input_id(1))
    fusion.on_event(
        make_event(K.RESULT, REMOTE, 1, b"remote", outcome=uriel.Outcome.DROP)
    )
    assert decided(fusion, 1) == b"remote"


def test_an_empty_forwarded_result_is_a_result(fusion):
    # An empty DetectionResult serializes to no bytes
    fusion.begin(input_id(1))
    result(fusion, REMOTE, 1, b"")
    assert decided(fusion, 1) == b""


def test_waits_for_remote_if_local_fails(fusion, clock):
    fusion.begin(input_id(1))
    fusion.on_event(make_event(K.TIMED_OUT, LOCAL, 1))
    clock.advance_ms(TIMEOUT_MS - 1)
    assert decided(fusion, 1) is None
    result(fusion, REMOTE, 1, b"remote")
    assert decided(fusion, 1) == b"remote"


@pytest.mark.parametrize(
    "local_kind, remote_kind",
    [(K.SKIPPED, K.TIMED_OUT), (K.TIMED_OUT, K.SKIPPED), (K.RESULT, K.RESULT)],
)
def test_dropped_if_both_fail(fusion, local_kind, remote_kind):
    fusion.begin(input_id(1))
    fusion.on_event(make_event(local_kind, LOCAL, 1, outcome=uriel.Outcome.DROP))
    assert decided(fusion, 1) is None
    fusion.on_event(make_event(remote_kind, REMOTE, 1, outcome=uriel.Outcome.DROP))
    assert decided(fusion, 1) == b"<drop>"


def test_dropped_if_both_disconnect(fusion):
    fusion.begin(input_id(1))
    fusion.on_event(make_event(K.DISCONNECTED, LOCAL))
    fusion.on_event(make_event(K.DISCONNECTED, REMOTE))
    assert decided(fusion, 1) == b"<drop>"


def test_a_disconnected_engine_fails_later_inputs_until_heard_from(fusion):
    fusion.on_event(make_event(K.DISCONNECTED, REMOTE))
    fusion.begin(input_id(1))
    result(fusion, LOCAL, 1, b"local")
    assert decided(fusion, 1) == b"local"
    fusion.end(input_id(1))

    fusion.on_event(make_event(K.ACCEPTED, REMOTE, 2))
    fusion.begin(input_id(2))
    result(fusion, LOCAL, 2, b"local")
    assert decided(fusion, 2) is None
    result(fusion, REMOTE, 2, b"remote")
    assert decided(fusion, 2) == b"remote"


def test_lost_events_fail_unfinished_inputs(fusion):
    fusion.begin(input_id(1))
    result(fusion, LOCAL, 1, b"local")
    fusion.on_event(make_event(K.LOST, REMOTE))
    assert decided(fusion, 1) == b"local"


def test_a_result_after_a_failure_counts(fusion):
    fusion.begin(input_id(1))
    fusion.on_event(make_event(K.LOST, REMOTE))
    fusion.on_event(make_event(K.LOST, LOCAL))
    result(fusion, REMOTE, 1, b"remote")
    assert decided(fusion, 1) == b"remote"


def test_events_of_other_engines_are_ignored(fusion):
    fusion.begin(input_id(1))
    result(fusion, "other", 1, b"other")
    fusion.on_event(make_event(K.DISCONNECTED, "other"))
    assert decided(fusion, 1) is None


def test_end_forgets_earlier_inputs_of_the_session(fusion):
    for seq in (1, 2, 3):
        result(fusion, LOCAL, seq, b"local")
    other = uriel.InputId(PRODUCER, SESSION + 1, 1)
    fusion.on_event(
        uriel.Event(
            kind=K.TIMED_OUT,
            lost=0,
            id=other,
            engine=LOCAL,
            flow="flow",
            outcome=uriel.Outcome.DROP,
            payload_type="",
            result=b"",
            at_ns=0,
            offered_at_ns=None,
            received_at_ns=None,
        )
    )
    fusion.begin(input_id(2))
    fusion.end(input_id(2))
    # Input 3 is still to come, and the other session is not ordered with it
    assert fusion.tracked == 2


def test_tracking_is_bounded_and_keeps_the_current_input(fusion, monkeypatch):
    monkeypatch.setattr(detection, "MAX_TRACKED", 4)
    fusion.begin(input_id(0))
    result(fusion, LOCAL, 0, b"local")
    for seq in range(1, 10):
        result(fusion, LOCAL, seq, b"local")
    assert fusion.tracked == 4
    fusion.on_event(make_event(K.TIMED_OUT, REMOTE, 0))
    assert decided(fusion, 0) == b"local"


def logged(caplog):
    return [record.getMessage() for record in caplog.records]


def test_logs_a_remote_result_in_time(fusion, clock, caplog):
    caplog.set_level(logging.INFO, "fusion.detection")
    fusion.begin(input_id(1))
    result(fusion, LOCAL, 1, b"local")
    clock.advance_ms(50)
    result(fusion, REMOTE, 1, b"remote")
    fusion.decide(input_id(1))
    assert logged(caplog) == [
        "input 1: local accepted first, waiting",
        "input 1: remote accepted, 50.0 ms after local",
        "input 1: returning remote result (in time)",
    ]


def test_logs_the_local_result_when_the_grace_period_ends_and_a_late_remote(
    fusion, clock, caplog
):
    caplog.set_level(logging.INFO, "fusion.detection")
    fusion.begin(input_id(1))
    result(fusion, LOCAL, 1, b"local")
    clock.advance_ms(GRACE_MS)
    fusion.decide(input_id(1))
    fusion.end(input_id(1))
    clock.advance_ms(30)
    result(fusion, REMOTE, 1, b"remote")
    assert logged(caplog) == [
        "input 1: local accepted first, waiting",
        "input 1: returning local result (grace period ended)",
        "input 1: remote accepted too late, 30.0 ms after returning local",
    ]
    assert fusion.tracked == 0


def test_logs_results_from_before_the_input_arrived_in_order(fusion, clock, caplog):
    caplog.set_level(logging.INFO, "fusion.detection")
    result(fusion, REMOTE, 1, b"remote")
    clock.advance_ms(5)
    result(fusion, LOCAL, 1, b"local")
    assert logged(caplog) == []
    fusion.begin(input_id(1))
    assert logged(caplog) == [
        "input 1: remote accepted first, waiting",
        "input 1: local accepted, 5.0 ms after remote",
    ]


def test_logs_no_result_if_neither_engine_accepts(fusion, caplog):
    caplog.set_level(logging.INFO, "fusion.detection")
    fusion.begin(input_id(1))
    fusion.on_event(make_event(K.TIMED_OUT, LOCAL, 1))
    fusion.on_event(make_event(K.SKIPPED, REMOTE, 1))
    fusion.decide(input_id(1))
    assert logged(caplog) == ["input 1: neither engine accepted, returning no result"]


def test_logs_nothing_for_inputs_that_never_arrive(fusion, caplog):
    caplog.set_level(logging.INFO, "fusion.detection")
    fusion.begin(input_id(7))
    result(fusion, LOCAL, 3, b"local")
    assert logged(caplog) == []


def test_remembered_decisions_are_bounded(fusion, monkeypatch):
    monkeypatch.setattr(detection, "MAX_DECIDED", 2)
    for seq in range(5):
        fusion.begin(input_id(seq))
        result(fusion, REMOTE, seq, b"remote")
        fusion.decide(input_id(seq))
        fusion.end(input_id(seq))
    assert len(fusion._decided) == 2


class RecordingEngine:
    def __init__(self):
        self.dropped = []

    def drop(self, *, result=b""):
        self.dropped.append(result)


@pytest.mark.parametrize(
    "payload_type, warned",
    [
        (DETECTION_RESULT_TYPE, False),
        (DETECTION_RESULT_TYPE.replace("/", "."), False),
        ("some/other/Type", True),
    ],
    ids=["slashes", "dots", "other"],
)
def test_warns_only_of_another_result_type(caplog, payload_type, warned):
    engine = RecordingEngine()
    decision = Report(Result(b"boxes", payload_type, 0), LOCAL)
    _report(engine, input_id(1), decision, DETECTION_RESULT_TYPE, 1024)
    assert engine.dropped == [b"boxes"]
    assert bool(caplog.records) == warned


def test_no_result_when_nothing_is_heard_by_the_timeout(fusion, clock, caplog):
    caplog.set_level(logging.INFO, "fusion.detection")
    fusion.begin(input_id(1))
    clock.advance_ms(TIMEOUT_MS - 1)
    assert decided(fusion, 1) is None
    clock.advance_ms(1)
    assert decided(fusion, 1) == b"<drop>"
    assert logged(caplog) == [
        "input 1: timed out after 1000 ms, returning no result (local pending, remote pending)"
    ]


def test_a_skipped_local_and_a_silent_remote_do_not_hold_up_the_next_input(
    fusion, clock, caplog
):
    caplog.set_level(logging.INFO, "fusion.detection")
    fusion.begin(input_id(1))
    fusion.on_event(make_event(K.SKIPPED, LOCAL, 1))
    clock.advance_ms(TIMEOUT_MS)
    assert decided(fusion, 1) == b"<drop>"
    assert "(local failed, remote pending)" in logged(caplog)[0]
    fusion.end(input_id(1))

    fusion.begin(input_id(2))
    result(fusion, REMOTE, 2, b"remote")
    assert decided(fusion, 2) == b"remote"


def test_timeout_is_counted_from_when_the_input_arrives(fusion, clock):
    fusion.on_event(make_event(K.SKIPPED, LOCAL, 1))
    clock.advance_ms(10 * TIMEOUT_MS)
    fusion.begin(input_id(1))
    assert decided(fusion, 1) is None


def test_timeout_shortens_the_grace_period(clock, caplog):
    caplog.set_level(logging.INFO, "fusion.detection")
    fusion = DetectionFusion(
        local=LOCAL,
        remote=REMOTE,
        grace_ns=500 * 1_000_000,
        timeout_ns=200 * 1_000_000,
        clock=clock,
    )
    fusion.begin(input_id(1))
    result(fusion, LOCAL, 1, b"local")
    assert fusion.decide(input_id(1)) == Wait(clock.ns + 200 * 1_000_000)
    clock.advance_ms(200)
    assert decided(fusion, 1) == b"local"
    assert logged(caplog)[-1] == "input 1: returning local result (timed out)"


def test_a_result_after_the_timeout_is_logged_too_late(fusion, clock, caplog):
    caplog.set_level(logging.INFO, "fusion.detection")
    fusion.begin(input_id(1))
    clock.advance_ms(TIMEOUT_MS)
    fusion.decide(input_id(1))
    fusion.end(input_id(1))
    clock.advance_ms(10)
    result(fusion, REMOTE, 1, b"remote")
    assert logged(caplog)[-1] == (
        "input 1: remote accepted too late, 10.0 ms after returning no result"
    )
