"""cliped's visual guard and propagation: a no is guarded only if its tile is
visually a tile said yes to, and each answer is also learned for the recent
tiles visually like the answered one."""
import numpy as np
from conftest import VERIFIER, BrightnessEncoder

from cliped import main
from cliped.clip import ADDED_BACKGROUND, CONFIRMED, GUARDED, Selector
from cliped.engine import Cliped
from cliped.send_queue import SendQueue, Tile
from test_learning import X, Y, unit

# CLIP-alike, as aerial tiles are: the brush is 0.93 to the mannequin, over
# the cosine guard, though it is a different object
MANNEQUIN, BRUSH, BRUSH2 = unit(1, 0.2, 0.5), unit(0.75, 0.25, 0.75), unit(0.74, 0.25, 0.76)


class Keypoints:
    """Stands in for similar.SimilarFilter: a tile's features are the name of
    the object it shows, and two tiles match if they show the same one."""
    min_matches = 40

    def matches(self, a, b):
        return 100 if a == b else 3

    def same(self, a, b, va=None, vb=None):
        return self.matches(a, b)


def make(visual_guard=True, propagate_s=20.0):
    sel = Selector(X, Y[None], learn=True, similarity=0.9, good_similarity=0.95, max_send=8)
    return Cliped(BrightnessEncoder(), sel, SendQueue(max_size=8), prompt="p", verifier=VERIFIER, tile_size=200,
                  stride=150, visual=Keypoints(), visual_guard=visual_guard, propagate_s=propagate_s)


def tile(frame_id, embedding, obj):
    return Tile(score=0.5, payload=b"", frame_id=frame_id, box=(0, 0, 200, 200), embedding=embedding, features=obj)


def recent(cliped, *tiles, now=0.0):
    cliped._last_now = now
    for t in tiles:
        cliped._recent.append((now, t))


def test_the_cosine_guard_keeps_a_no_from_being_learned_near_a_yes():
    sel = Selector(X, Y[None], learn=True, similarity=0.9, good_similarity=0.95)
    sel.learn(MANNEQUIN, True)
    assert float(BRUSH @ MANNEQUIN) > 0.9
    assert sel.learn(BRUSH, False) == GUARDED
    # The caller's word replaces it
    assert sel.learn(BRUSH, False, guarded=False) == ADDED_BACKGROUND
    assert sel.learn(BRUSH2, False, guarded=True) == GUARDED


def test_with_the_visual_guard_a_no_is_learned_unless_its_tile_is_a_yes_tiles_object():
    cliped = make(propagate_s=0)
    cliped._learn_one(tile(1, MANNEQUIN, "mannequin"), True)
    cliped._learn_one(tile(5, MANNEQUIN, "mannequin"), True)  # confirmed, so a no can't drop it
    assert cliped._learn_one(tile(2, BRUSH, "brush"), False) == ADDED_BACKGROUND
    assert cliped._learn_one(tile(3, BRUSH2, "brush"), False) == CONFIRMED
    # A no about the mannequin itself, the verifier missing it, is guarded
    assert cliped._learn_one(tile(4, MANNEQUIN, "mannequin"), False) == GUARDED
    assert not cliped.selector.passes(BRUSH)[0] and cliped.selector.passes(MANNEQUIN)[0]


def test_without_it_the_cosine_guard_holds():
    cliped = make(visual_guard=False, propagate_s=0)
    cliped._learn_one(tile(1, MANNEQUIN, "mannequin"), True)
    assert cliped._learn_one(tile(2, BRUSH, "brush"), False) == GUARDED


def test_an_answer_is_propagated_to_recent_tiles_of_the_same_object():
    cliped = make()
    cliped._learn_one(tile(1, MANNEQUIN, "mannequin"), True)
    answered, like, other = tile(10, BRUSH, "brush"), tile(11, BRUSH2, "brush"), tile(12, MANNEQUIN, "mannequin")
    recent(cliped, answered, like, other)
    assert [(t.frame_id, n) for t, n in cliped._like(answered)] == [(11, 100)]
    # One no about the brush confirms it, through its look-alike
    assert cliped._learn_one(answered, False) == ADDED_BACKGROUND
    assert cliped._learn_one(like, False) == CONFIRMED
    assert not cliped.selector.passes(BRUSH)[0]


def test_only_unanswered_tiles_of_this_prompt_within_the_window_are_propagated_to():
    cliped = make(propagate_s=20)
    answered = tile(10, BRUSH, "brush")
    old, done, stale = tile(1, BRUSH2, "brush"), tile(2, BRUSH2, "brush"), tile(3, BRUSH2, "brush")
    done.answered = True
    stale.generation = -1
    recent(cliped, old, now=0.0)
    recent(cliped, done, stale, answered, now=25.0)
    assert cliped._like(answered) == []
    assert old not in [t for _, t in cliped._recent]


def test_learn_logs_and_applies_propagation(caplog):
    import json

    import uriel
    from test_learning import result_event

    cliped = make()
    cliped.queue.push(t := tile(10, BRUSH, "brush"))
    like = tile(11, BRUSH2, "brush")
    recent(cliped, t, like)
    from conftest import input_id

    t.carriers.append(input_id(1))
    with caplog.at_level("INFO", logger="cliped"):
        cliped.learn(t, result_event(1, {"match": False}))
    assert t.answered and cliped.selector.noes == [2]
    assert "also learned for the tiles like it of frames 11" in caplog.text


def test_args_visual_guard_and_propagate_s():
    import json

    import pytest

    args = main.parse_args(json.dumps({"prompt": "p", "verifier": "v"}))
    assert args["visual_guard"] is True and args["propagate_s"] == 20.0
    assert main.parse_args(json.dumps({"prompt": "p", "verifier": "v", "propagate_s": 0}))["propagate_s"] == 0.0
    for bad in [{"propagate_s": -1}, {"propagate_s": True}, {"visual_guard": "yes"}]:
        with pytest.raises(main.ConfigError):
            main.parse_args(json.dumps({"prompt": "p", "verifier": "v", **bad}))
