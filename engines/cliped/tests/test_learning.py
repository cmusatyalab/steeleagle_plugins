"""Learning from the verifier's answers: the gate of clip.Selector, the
queue's rescoring, and the engine reading the verifier's RESULT."""
import json

import numpy as np
import pytest
import uriel
from conftest import VERIFIER, BrightnessEncoder, input_id, make_input

from cliped.clip import ADDED_BACKGROUND, ADDED_GOOD, CONFIRMED, GUARDED, KNOWN, Selector
from cliped.engine import GENERIC_RESULT_TYPE, Cliped, read_verdict
from cliped.send_queue import SendQueue, Tile
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.result import result_pb2

K = uriel.EventKind


def unit(*x):
    x = np.array(x, dtype=np.float64)
    return x / np.linalg.norm(x)


def result_event(seq, verdict, payload_type=GENERIC_RESULT_TYPE):
    return uriel.Event(
        kind=K.RESULT, lost=0, id=input_id(seq), engine=VERIFIER, flow="flow", outcome=0,
        payload_type=payload_type,
        result=result_pb2.GenericResult(payload=json.dumps(verdict).encode()).SerializeToString(),
        at_ns=0, offered_at_ns=None, received_at_ns=None,
    )


# 3-D stand-ins: t = x, the one background = y
X, Y = unit(1, 0, 0), unit(0, 1, 0)
GROUND, TARGET = unit(0.3, 0.1, 1), unit(1, 0.2, 0.5)


def selector(**kwargs):
    return Selector(X, Y[None], learn=True, **{"similarity": 0.9, **kwargs})


def test_a_no_gates_tiles_like_it_only_once_confirmed():
    sel = selector()
    near = unit(0.32, 0.1, 1)
    assert sel.passes(near)[0]
    assert sel.learn(GROUND, False) == ADDED_BACKGROUND
    # One no could be the verifier missing a target: it gates nothing yet
    assert sel.passes(near)[0]
    assert sel.learn(unit(0.31, 0.1, 1), False) == CONFIRMED
    assert not sel.passes(near)[0]
    assert len(sel.backgrounds) == 1 and sel.noes == [2]
    # A further no to the same thing changes nothing
    assert sel.learn(near, False) == KNOWN


def test_a_yes_sends_tiles_like_it_whatever_their_score():
    sel = selector()
    weak = unit(0.1, 1, 0.5)  # looks more like the background than the prompt: s < 0
    s, verdict = sel.score(weak)
    assert s[0] < 0 and not sel.passes(weak)[0]
    assert sel.learn(unit(0.12, 1, 0.5), True) == ADDED_GOOD
    assert sel.passes(weak)[0]
    assert sel.learn(weak, True) == KNOWN


def test_the_learned_tiles_leave_s_alone():
    sel = selector(confirmations=1)
    sel.learn(GROUND, False)
    sel.learn(TARGET, True)
    tiles = np.stack([GROUND, TARGET, unit(0.5, 1, 0.5)])
    s, verdict = sel.score(tiles)
    assert np.allclose(s, Selector(X, Y[None]).relevance(tiles))
    assert list(verdict) == [-1, 1, 0]


def test_a_no_that_looks_like_a_good_tile_is_guarded():
    sel = selector()
    sel.learn(TARGET, True)
    # No background tiles yet: guarded if within similarity of a good tile
    assert sel.learn(unit(1, 0.25, 0.5), False) == GUARDED
    assert sel.learn(GROUND, False) == ADDED_BACKGROUND
    # Closer to a good tile than to any background tile, though not that close
    assert sel.learn(unit(1, 0.2, 0.0), False) == GUARDED
    assert len(sel.backgrounds) == 1


def test_with_nothing_learned_the_first_no_is_learned():
    assert selector().learn(TARGET, False) == ADDED_BACKGROUND


def test_at_most_max_learned_are_kept_oldest_first_out():
    sel = selector(similarity=0.99, max_learned=2)
    for x in [unit(0, 1, 1), unit(0, 0.5, 1), unit(0, 0.2, 1)]:
        assert sel.learn(x, False) == ADDED_BACKGROUND
    assert len(sel.backgrounds) == len(sel.noes) == 2 and np.allclose(sel.backgrounds[0], unit(0, 0.5, 1))


def test_select_sends_a_tile_like_a_yes_and_never_one_like_a_confirmed_no():
    sel = Selector(X, Y[None], learn=True, similarity=0.9, confirmations=1, k=1, margin=0.0, max_send=2)
    sel.learn(unit(0, 0.5, 1), True)
    sel.learn(unit(1, 0, 0), False)
    # The first would win on s, the third does not pass at all
    picked, _ = sel.select(np.stack([unit(1, 0.05, 0), unit(0, 0.55, 1), unit(0.1, 1, 0)]))
    assert list(picked) == [1]


def test_without_learning_nothing_is_gated():
    sel = Selector(X, Y[None])
    assert not sel.learning
    s, verdict = sel.score(np.stack([GROUND, TARGET]))
    assert (verdict == 0).all()


def test_rescore_drops_waiting_tiles_that_no_longer_pass_and_reorders_the_rest():
    queue = SendQueue(max_size=8, resend_after_s=5.0)
    a, b, c = (Tile(score=s, payload=b"", frame_id=i, box=(0, 0, 1, 1)) for i, s in enumerate([0.3, 0.2, 0.1]))
    for tile in (a, b, c):
        queue.push(tile)
    assert queue.next(carrier=uriel.InputId("p", 1, 1), now=0) is a
    new = {0: (False, -1.0, False), 1: (False, -0.5, False), 2: (True, 0.9, False)}
    dropped = queue.rescore(lambda t: new[t.frame_id])
    # a is in flight, so the verifier may have it: it stays
    assert dropped == [b] and len(queue) == 2 and (a.score, c.score) == (-1.0, 0.9)
    assert queue.next(carrier=uriel.InputId("p", 1, 2), now=0) is c


def test_must_sends_go_ahead_of_better_scored_tiles_and_are_dropped_last():
    queue = SendQueue(max_size=2, resend_after_s=5.0)
    strong = Tile(score=0.9, payload=b"", frame_id=1, box=(0, 0, 1, 1))
    must = Tile(score=0.1, payload=b"", frame_id=2, box=(0, 0, 1, 1), must_send=True)
    queue.push(strong)
    queue.push(must)
    # Full: a third tile, even the best scored, evicts the weaker non-must
    assert queue.push(Tile(score=0.95, payload=b"", frame_id=3, box=(0, 0, 1, 1))) is strong
    assert queue.next(carrier=uriel.InputId("p", 1, 1), now=0) is must


def test_rescore_moves_tiles_like_a_yes_to_the_front():
    queue = SendQueue(max_size=8, resend_after_s=5.0)
    tiles = [Tile(score=s, payload=b"", frame_id=i, box=(0, 0, 1, 1)) for i, s in enumerate([0.5, 0.4, 0.1])]
    for tile in tiles:
        queue.push(tile)
    queue.rescore(lambda t: (True, t.score, t.frame_id == 2))
    assert tiles[2].must_send and queue.next(carrier=uriel.InputId("p", 1, 1), now=0) is tiles[2]
    assert queue.next(carrier=uriel.InputId("p", 1, 2), now=0) is tiles[0]


def test_read_verdict():
    assert read_verdict(result_event(1, {"match": True})) is True
    assert read_verdict(result_event(1, {"match": False})) is False
    assert read_verdict(result_event(1, {"match": "yes"})) is None
    assert read_verdict(result_event(1, {"match": True}, payload_type="")) is None


@pytest.fixture
def learning_cliped():
    encoder = BrightnessEncoder()
    return Cliped(
        encoder,
        Selector(encoder.t, encoder.b, k=4, margin=0.0, max_send=8, learn=True, similarity=0.99),
        SendQueue(max_size=8, resend_after_s=5.0),
        prompt="a white square",
        verifier=VERIFIER,
        tile_size=200,
        stride=150,
    )


def gray_frame(id):
    """A frame whose bottom row of tiles is gray (s ~ 0.28) and top row half
    gray (s < 0): the three bottom tiles are queued."""
    pixels = np.zeros((300, 400, 3), np.uint8)
    pixels[100:] = 153
    return frame_pb2.RawFrame(data=pixels.tobytes(), h_res=400, v_res=300, channels=3, id=id).SerializeToString()


def test_confirmed_no_results_teach_the_selector_and_rescore_the_queue(learning_cliped):
    cliped = learning_cliped
    cliped.on_input(make_input(1, gray_frame(1)), now=0)
    cliped.on_input(make_input(2, gray_frame(2)), now=0)
    assert len(cliped.queue) == 6 and cliped.queue.in_flight == 2
    # The verifier says no to the first gray tile: learned, but not yet gating
    cliped.on_event(result_event(1, {"match": False}))
    assert cliped.selector.noes == [1] and len(cliped.queue) == 5
    # A second no to the same gray confirms it: the waiting gray tiles go
    cliped.on_event(result_event(2, {"match": False}))
    assert cliped.selector.noes == [2] and len(cliped.queue) == 0 and cliped.queue.confirmed == 2


def test_a_yes_puts_queued_tiles_like_it_first_and_new_ones_too(learning_cliped):
    cliped = learning_cliped
    # A dimmer gray (s lower than the bright gray's) and a brighter one
    def frame(level, id):
        pixels = np.zeros((300, 400, 3), np.uint8)
        pixels[100:] = level
        return frame_pb2.RawFrame(data=pixels.tobytes(), h_res=400, v_res=300, channels=3, id=id).SerializeToString()
    cliped.on_input(make_input(1, frame(140, 1)), now=0)  # sends one dim tile, queues two
    cliped.on_input(make_input(2, frame(200, 2)), now=0)  # queues bright ones, which beat the dim on s
    # The verifier says yes to the dim tile: the dim tiles waiting go first
    cliped.on_event(result_event(1, {"match": True}))
    nxt = cliped.queue.next(carrier=input_id(3), now=0)
    assert nxt.frame_id == 1 and nxt.must_send
    # A new dim frame's tiles are queued as must-sends too
    cliped.on_input(make_input(4, frame(140, 4)), now=0)
    assert any(t.must_send and t.frame_id == 4 for t in cliped.queue._tiles)


def test_a_result_that_is_not_a_verdict_teaches_nothing(learning_cliped):
    cliped = learning_cliped
    cliped.on_input(make_input(1, gray_frame(1)), now=0)
    cliped.on_event(result_event(1, {"match": False}, payload_type="steeleagle_protocol/v1/messages/result/DetectionResult"))
    assert cliped.queue.confirmed == 1 and not cliped.selector.backgrounds
