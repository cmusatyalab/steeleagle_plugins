import pytest
import uriel
from conftest import REMOTE, make_event, make_frame, make_input

from steeleagle_protocol.v1.messages.frame import frame_pb2
from storage import storage_pb2
from storage.reachback import REACHBACK_REQUEST_TYPE, Reachback

K = uriel.EventKind


@pytest.fixture
def engine(store):
    engine = Reachback(store, remote=REMOTE, max_frame_size=1 << 20)
    for seq, x, y in ((1, 0.0, 0.0), (2, 10.0, 0.0), (3, 10.0, 10.0)):
        assert engine.on_input(make_input(seq, make_frame(x=x, y=y, data=f"f{seq}".encode()))) is None
    return engine


def request(engine, payload_type=REACHBACK_REQUEST_TYPE, **fields):
    req = storage_pb2.ReachbackRequest(**fields)
    engine.on_event(make_event(K.DERIVED, REMOTE, 0, req.SerializeToString(), payload_type))


def sent(engine, seq):
    out = engine.on_input(make_input(seq, make_frame(data=f"f{seq}".encode())))
    return None if out is None else frame_pb2.EncodedFrame.FromString(out.data).encoded_data


def test_frames_are_stored_and_dropped(engine, store):
    assert len(store) == 3
    assert sent(engine, 4) is None
    assert len(store) == 4


def test_a_sequence_number_picks_that_frame(engine):
    request(engine, sequence_number=1, x=10.0, y=10.0)
    assert sent(engine, 4) == b"f1"
    assert sent(engine, 5) is None


def test_otherwise_the_lowest_mean_squared_error_wins(engine):
    request(engine, x=9.0, y=1.0)
    request(engine, x=8.0, y=9.0)
    assert sent(engine, 4) == b"f2"
    assert sent(engine, 5) == b"f3"


def test_bad_or_unmatched_requests_send_nothing(engine):
    request(engine, sequence_number=99)
    request(engine, latitude=1.0)
    request(engine)
    request(engine, payload_type="other/v1/Thing", x=0.0)
    engine.on_event(make_event(K.DERIVED, REMOTE, 0, b"\xff\xff", REACHBACK_REQUEST_TYPE))
    engine.on_event(make_event(K.RESULT, REMOTE, 0, b"", ""))
    assert sent(engine, 4) is None
