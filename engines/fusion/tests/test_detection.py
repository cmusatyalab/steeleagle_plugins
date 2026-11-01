import pytest
import uriel
from conftest import LOCAL, PRODUCER, REMOTE, SESSION, Clock, input_id, make_event

from fusion import detection
from fusion.detection import DetectionFusion, Report, Wait

K = uriel.EventKind
GRACE_MS = 100


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fusion(clock):
    return DetectionFusion(
        local=LOCAL, remote=REMOTE, grace_ns=GRACE_MS * 1_000_000, clock=clock
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
        DetectionFusion(local="a", remote="a", grace_ns=0, clock=Clock())


def test_waits_until_an_engine_reports(fusion):
    fusion.begin(input_id(1))
    assert fusion.decide(input_id(1)) == Wait(None)
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
    clock.advance_ms(10 * GRACE_MS)
    assert fusion.decide(input_id(1)) == Wait(None)
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
