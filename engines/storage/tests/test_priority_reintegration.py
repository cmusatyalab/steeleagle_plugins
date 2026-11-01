import pytest
import uriel
from conftest import CLASSIFIER, REMOTE, make_event, make_frame, make_input

from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.result import result_pb2
from storage.priority_reintegration import Mode, PriorityReintegration

K = uriel.EventKind
CLASSIFICATION_TYPE = "steeleagle_protocol/v1/messages/result/ClassificationResult"


@pytest.fixture
def engine(store):
    return PriorityReintegration(
        store,
        remote=REMOTE,
        classifier=CLASSIFIER,
        target_class="Person",
        max_frame_size=1 << 20,
    )


def classified(engine, seq, cls, score):
    result = result_pb2.ClassificationResult(score=score)
    setattr(result, "class", cls)
    engine.on_event(
        make_event(K.RESULT, CLASSIFIER, seq, result.SerializeToString(), CLASSIFICATION_TYPE)
    )


def remote(engine, kind, seq=0):
    engine.on_event(make_event(kind, REMOTE, seq))


def frame_of(stored):
    return frame_pb2.EncodedFrame.FromString(stored.data).encoded_data


def store_frames(engine, scores):
    """Disconnects, stores a frame per (seq, class, score) and classifies it."""
    remote(engine, K.DISCONNECTED)
    for seq, cls, score in scores:
        assert engine.on_input(make_input(seq, make_frame(data=f"f{seq}".encode()))) is None
        classified(engine, seq, cls, score)


def test_frames_are_dropped_and_not_stored_until_a_disconnect(engine, store):
    assert engine.on_input(make_input(1)) is None
    classified(engine, 1, "person", 0.9)
    assert len(store) == 0 and engine.queued == 0
    remote(engine, K.DISCONNECTED)
    assert engine.mode == Mode.STORING
    assert engine.on_input(make_input(2)) is None
    assert len(store) == 1


def test_only_the_target_class_is_queued(engine, store):
    store_frames(engine, [(1, "person", 0.5), (2, "car", 0.9), (3, "PERSON", 0.7)])
    assert len(store) == 3
    assert engine.queued == 2


def test_frames_are_reintegrated_highest_score_first_one_at_a_time(engine):
    store_frames(engine, [(1, "person", 0.2), (2, "person", 0.9), (3, "person", 0.5)])
    remote(engine, K.ACCEPTED, 100)
    assert engine.mode == Mode.REINTEGRATING

    sent = engine.on_input(make_input(100))
    assert frame_of(sent) == b"f2"
    # Waiting for its SENT: inputs are dropped
    assert engine.on_input(make_input(101)) is None
    remote(engine, K.SENT, 100)
    assert frame_of(engine.on_input(make_input(102))) == b"f3"
    remote(engine, K.SENT, 102)
    assert frame_of(engine.on_input(make_input(103))) == b"f1"
    remote(engine, K.SENT, 103)
    assert engine.on_input(make_input(104)) is None


def test_reintegrating_does_not_store_new_frames(engine, store):
    store_frames(engine, [(1, "person", 0.9)])
    remote(engine, K.RESULT, 50)
    engine.on_input(make_input(60))
    assert len(store) == 1


@pytest.mark.parametrize("kind", [K.SKIPPED, K.TIMED_OUT])
def test_a_frame_not_sent_is_retried(engine, kind):
    store_frames(engine, [(1, "person", 0.9), (2, "person", 0.1)])
    remote(engine, K.SENT, 99)
    assert frame_of(engine.on_input(make_input(100))) == b"f1"
    # An event about another input does not count
    remote(engine, kind, 42)
    assert engine.on_input(make_input(101)) is None
    remote(engine, kind, 100)
    assert frame_of(engine.on_input(make_input(102))) == b"f1"
    remote(engine, K.SENT, 102)
    assert frame_of(engine.on_input(make_input(103))) == b"f2"


def test_lost_events_retry_the_frame(engine):
    store_frames(engine, [(1, "person", 0.9)])
    remote(engine, K.SENT, 99)
    engine.on_input(make_input(100))
    remote(engine, K.LOST)
    assert frame_of(engine.on_input(make_input(101))) == b"f1"


def test_a_disconnect_returns_to_storing_and_requeues_the_frame(engine, store):
    store_frames(engine, [(1, "person", 0.9), (2, "person", 0.5)])
    remote(engine, K.SENT, 99)
    assert frame_of(engine.on_input(make_input(100))) == b"f1"
    remote(engine, K.DISCONNECTED)
    assert engine.mode == Mode.STORING
    assert engine.on_input(make_input(101, make_frame(data=b"f101"))) is None
    classified(engine, 101, "person", 0.7)
    assert len(store) == 3

    remote(engine, K.SENT, 200)
    assert frame_of(engine.on_input(make_input(201))) == b"f1"
    remote(engine, K.SENT, 201)
    assert frame_of(engine.on_input(make_input(202))) == b"f101"


def test_a_late_classification_of_a_stored_frame_is_queued(engine):
    remote(engine, K.DISCONNECTED)
    engine.on_input(make_input(1, make_frame(data=b"f1")))
    remote(engine, K.SENT, 10)
    classified(engine, 1, "person", 0.8)
    assert frame_of(engine.on_input(make_input(11))) == b"f1"


def test_other_inputs_and_results_are_ignored(engine, store):
    remote(engine, K.DISCONNECTED)
    assert engine.on_input(make_input(1, payload_type="RawFrame")) is None
    engine.on_event(make_event(K.RESULT, CLASSIFIER, 1, b"\x01", "DetectionResult"))
    assert len(store) == 0 and engine.queued == 0
