"""clic_tile_search's visual guard and propagation: a no is guarded only if its tile is
visually a tile said yes to, and each answer is also learned for the recent
tiles visually like the answered one."""
import json

import numpy as np
import pytest
from conftest import VERIFIER, BrightnessEncoder

from clic import main
from clic.clip import ADDED_BACKGROUND, ADDED_GOOD, CONFIRMED, DROPPED_GOOD, GUARDED, KNOWN, Selector
from clic.engine import Cliped
from clic.send_queue import SendQueue, Tile
from test_learning import X, Y, unit

# CLIP-alike, as aerial tiles are: the brush is 0.93 to the mannequin, over
# the cosine guard, though it is a different object
MANNEQUIN, BRUSH, BRUSH2 = unit(1, 0.2, 0.5), unit(0.75, 0.25, 0.75), unit(0.74, 0.25, 0.76)


class Keypoints:
    """Stands in for similar.SimilarFilter: a tile's features are the name of
    the object it shows, and two tiles match if they show the same one."""
    min_matches = 40

    def keypoints(self, features):
        return 0 if features == "blank" else 100

    def matches(self, a, b):
        return 0 if "blank" in (a, b) else 100 if a == b else 3

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
    # A copied no adds nothing to the brush's one no; a second verifier no confirms it
    assert cliped._learn_one(answered, False) == ADDED_BACKGROUND
    assert cliped._learn_one(like, False, counted=False) == KNOWN
    assert cliped.selector.noes == [1] and cliped.selector.passes(BRUSH)[0]
    assert cliped._learn_one(like, False) == CONFIRMED
    assert not cliped.selector.passes(BRUSH)[0]


def test_a_copied_answer_never_confirms_or_drops_a_tile():
    sel = Selector(X, Y[None], learn=True, similarity=0.9, good_similarity=0.95)
    assert sel.learn(MANNEQUIN, True) == ADDED_GOOD
    assert sel.learn(MANNEQUIN, True, counted=False) == KNOWN and sel.yeses == [1]
    assert sel.learn(MANNEQUIN, False, counted=False) == KNOWN and len(sel.good) == 1 and sel.good_noes == [0]
    # Away from what it knows, a copied answer still adds its tile, with one answer
    assert sel.learn(BRUSH, False, guarded=False, counted=False) == ADDED_BACKGROUND and sel.noes == [1]


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
    with caplog.at_level("INFO", logger="clic"):
        cliped.learn(t, result_event(1, {"match": False}))
    # The look-alike's copy of the no doesn't count: one no, and nothing dropped
    assert t.answered and cliped.selector.noes == [1]
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


# box_rerank has the same visual guard and propagation over its crops


def make_reranker(visual_guard=True, propagate_s=20.0, lift_vetoes=True):
    from clic.rerank import Reranker

    sel = Selector(X, Y[None], learn=True, similarity=0.9, good_similarity=0.95)
    return Reranker(BrightnessEncoder(), sel, SendQueue(max_size=8), detector="yolo", verifier=VERIFIER,
                    class_name="dog", visual=Keypoints(), visual_guard=visual_guard, propagate_s=propagate_s,
                    lift_vetoes=lift_vetoes)


def test_box_rerank_guards_a_no_visually():
    rr = make_reranker(propagate_s=0)
    rr._learn_one(tile(1, MANNEQUIN, "mannequin"), True)
    rr._learn_one(tile(5, MANNEQUIN, "mannequin"), True)
    assert rr._learn_one(tile(2, BRUSH, "brush"), False) == ADDED_BACKGROUND
    assert rr._learn_one(tile(4, MANNEQUIN, "mannequin"), False) == GUARDED
    assert make_reranker(visual_guard=False, propagate_s=0)._learn_one(tile(2, BRUSH, "brush"), False) == \
        ADDED_BACKGROUND  # nothing said yes to yet, so no cosine guard either


def test_box_rerank_propagates_to_recent_crops_of_the_same_object():
    rr = make_reranker()
    answered, like, old = tile(10, BRUSH, "brush"), tile(11, BRUSH2, "brush"), tile(3, BRUSH2, "brush")
    rr._recent.extend([(0.0, old), (25.0, answered), (26.0, like)])
    assert [(t.frame_id, n) for t, n in rr._like(answered, 30.0)] == [(11, 100)]
    assert [t.frame_id for _, t in rr._recent] == [10, 11]  # older than propagate_s is forgotten


def test_smart_search_never_lifts_a_veto_by_keypoints():
    for lift, unverified in ((True, True), (False, False)):
        rr = make_reranker(propagate_s=0, lift_vetoes=lift)
        rr._learn_one(tile(1, BRUSH, "water"), False)
        assert rr._learn_one(tile(2, BRUSH2, "water"), False) == CONFIRMED
        # A crop CLIP vetoes, visually unlike the background: lifted by clic_box_rerank only
        assert rr._still_unverified(tile(3, BRUSH2, "swimmer"))[0] is unverified
        assert rr._still_unverified(tile(4, BRUSH2, "water"))[0] is False


def test_smart_search_defaults_are_run_2_3_13s_and_box_rerank_refuses_its_args():
    base = {"detector": "yolo", "verifier": "v", "class": "person", "prompt": "person"}
    smart = main.parse_rerank_args(json.dumps(base), smart=True)
    assert (smart["classes"], smart["max_candidates"], smart["verify_context"], smart["min_verify_side"],
            smart["max_verify_side"], smart["max_queue"], smart["defer_similar"], smart["score_jump"],
            smart["score_jump_floor"], smart["prompt_filter"], smart["min_prompt_score"],
            smart["dedupe_overlap"]) == (["object"], 5, 1.25, 48, 128, 512, True, 3.0, 6.4, True, 0.0, 0.8)
    box = main.parse_rerank_args(json.dumps(base))
    assert (box["classes"], box["prompt_filter"], box["dedupe_overlap"], box["score_jump"]) == \
        (["person"], False, 0.0, 0.0)
    with pytest.raises(main.ConfigError, match="only clic_smart_search"):
        main.parse_rerank_args(json.dumps({**base, "prompt_filter": True}))
    assert (smart["repeat_matches"], smart["repeat_window"], smart["repeat_side"]) == (10, 0.0, 96)
    assert box["repeat_matches"] == 0
    with pytest.raises(main.ConfigError, match="only clic_smart_search"):
        main.parse_rerank_args(json.dumps({**base, "repeat_matches": 10}))
    assert main.parse_verifier_args('{"prompt": "p"}')["no_conf"] == 0.95


def test_box_rerank_visual_args_are_defaulted():
    args = main.parse_rerank_args('{"detector": "yolo", "verifier": "v", "class": "dog", "prompt": "a dog"}')
    assert (args["visual_guard"], args["propagate_s"], args["defer_similar"], args["defer_similar_matches"]) == \
        (True, 20.0, False, 40)


# With the visual guard, a confirmed background vetoes only tiles visually its own patch, and a no cancels a
# candidate yes only if it is visually that tile: CLIP can't tell a swimmer's tile from empty water's

WATER_LIKE = unit(0.5, 0.6, 0.7)  # ~0.94 to the brush, but s <= 0: never relevant


def test_with_the_visual_guard_a_background_vetoes_only_tiles_of_its_own_patch():
    cliped = make(propagate_s=0)
    cliped._learn_one(tile(1, BRUSH, "water"), False)
    assert cliped._learn_one(tile(2, BRUSH2, "water"), False) == CONFIRMED
    assert cliped.selector.background_features == [["water", "water"]]
    swimmer, water = tile(3, BRUSH2, "swimmer"), tile(4, BRUSH2, "water")
    assert cliped.selector.score(BRUSH2)[1][0] == -1  # CLIP alone vetoes both
    assert cliped.selector.passes(swimmer.embedding, cliped._unveto(swimmer))[0]
    assert not cliped.selector.passes(water.embedding, cliped._unveto(water))[0]


def test_select_asks_about_keypoints_only_for_the_relevant_tiles_it_would_veto():
    sel = Selector(X, Y[None], learn=True, similarity=0.9, good_similarity=0.95, max_send=8, k=3, margin=0)
    sel.learn(BRUSH, False, guarded=False, features="water")
    sel.learn(BRUSH2, False, guarded=False, features="water")
    asked = []

    def unveto(i, features):
        asked.append((i, features))
        return True

    picked, s = sel.select(np.array([BRUSH2, WATER_LIKE, Y]), unveto)
    assert asked == [(0, ["water", "water"])] and list(picked) == [0]
    assert list(sel.select(np.array([BRUSH2, WATER_LIKE, Y]))[0]) == []  # without it, CLIP vetoes


def test_with_the_visual_guard_a_no_unlike_a_candidate_yes_does_not_drop_it():
    sel = Selector(X, Y[None], learn=True, similarity=0.9, good_similarity=0.95)
    assert sel.learn(MANNEQUIN, True) == ADDED_GOOD
    assert sel.learn(MANNEQUIN, False, guarded=False) == ADDED_BACKGROUND and len(sel.good) == 1
    assert sel.learn(MANNEQUIN, False, guarded=True) == DROPPED_GOOD and sel.good == []


def test_box_rerank_vetoes_only_crops_of_the_backgrounds_own_patch():
    rr = make_reranker(propagate_s=0)
    rr._learn_one(tile(1, BRUSH, "water"), False)
    rr._learn_one(tile(2, BRUSH2, "water"), False)
    assert rr._still_unverified(tile(3, BRUSH2, "swimmer"))[0]
    assert not rr._still_unverified(tile(4, BRUSH2, "water"))[0]


def test_a_featureless_tile_stays_vetoed_by_its_confirmed_background():
    # Black padding or a washed-out tile has no keypoints, so it can never
    # match the background's; without this, the veto could never apply and
    # such tiles would flood the queue
    cliped = make(propagate_s=0)
    cliped._learn_one(tile(1, BRUSH, "blank"), False)
    assert cliped._learn_one(tile(2, BRUSH2, "blank"), False) == CONFIRMED
    blank = tile(3, BRUSH2, "blank")
    assert not cliped.selector.passes(blank.embedding, cliped._unveto(blank))[0]
    assert not cliped._lifts(None, ["water"])


def test_box_rerank_keeps_a_featureless_crop_vetoed():
    rr = make_reranker(propagate_s=0)
    rr._learn_one(tile(1, BRUSH, "blank"), False)
    rr._learn_one(tile(2, BRUSH2, "blank"), False)
    assert not rr._still_unverified(tile(3, BRUSH2, "blank"))[0]
