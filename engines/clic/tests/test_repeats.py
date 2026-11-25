"""clic_smart_search's repeat check (similar.SentMemory): the same object is
not sent twice unless its score jumps."""
import cv2
import numpy as np

from clic.similar import SentMemory

V = np.array([1.0, 0.0, 0.0], dtype=np.float32)
OTHER_V = np.array([0.0, 1.0, 0.0], dtype=np.float32)


def scene(seed, size=400):
    rng = np.random.default_rng(seed)
    img = cv2.resize(rng.integers(0, 255, (size // 8, size // 8, 3), dtype=np.uint8), (size, size),
                     interpolation=cv2.INTER_NEAREST)
    return cv2.GaussianBlur(img, (3, 3), 0)


def crop(img, x, y, side=48):
    return np.ascontiguousarray(img[y:y + side, x:x + side])


def test_a_small_crop_has_keypoints_only_scaled_up():
    mem = SentMemory()
    small = crop(scene(1), 100, 100)
    native = SentMemory(side=1).features(small)[1]
    assert len(mem.features(small)[1]) >= 3 * (0 if native is None else len(native))


def test_the_same_object_shifted_is_a_repeat_and_another_is_not():
    mem, img = SentMemory(), scene(1)
    first = mem.features(crop(img, 100, 100))
    mem.sent(None, V, first, 5.0, 0.0, 1)
    assert mem.like(V, mem.features(crop(img, 104, 102)), 1.0) is not None
    assert mem.like(V, mem.features(crop(scene(2), 100, 100)), 1.0) is None
    # Unlike in CLIP: not compared at all
    assert mem.like(OTHER_V, mem.features(crop(img, 104, 102)), 1.0) is None


def test_the_window_forgets_and_zero_window_remembers_the_whole_flight():
    img = scene(1)
    for window, later, found in ((30.0, 31.0, False), (30.0, 29.0, True), (None, 1e5, True)):
        mem = SentMemory(window_s=window)
        mem.sent(None, V, mem.features(crop(img, 100, 100)), 5.0, 0.0, 1)
        assert (mem.like(V, mem.features(crop(img, 102, 100)), later) is not None) == found


def test_an_entry_follows_its_object_and_keeps_the_last_sent_score():
    mem, img = SentMemory(), scene(1)
    mem.sent(None, V, mem.features(crop(img, 100, 100)), 5.0, 0.0, 1)
    e = mem.like(V, mem.features(crop(img, 112, 100)), 1.0)
    mem.held_back(e, V, mem.features(crop(img, 112, 100)), 1.0)
    # Drifted further than the first view would match, but the entry followed it
    assert mem.like(V, mem.features(crop(img, 124, 100)), 2.0) is e
    assert e[2] == 5.0
    mem.sent(e, V, mem.features(crop(img, 124, 100)), 20.0, 2.0, 3)
    assert (e[2], e[4], len(mem._entries)) == (20.0, 3, 1)


def test_memory_is_bounded_and_forget_clears_it():
    mem = SentMemory(memory=2)
    for i in range(3):
        mem.sent(None, V, mem.features(crop(scene(i), 100, 100)), 1.0, float(i), i)
    assert [e[4] for e in mem._entries] == [1, 2]
    mem.forget()
    assert mem._entries == []


def test_the_adaptive_bar_climbs_over_steady_clutter_and_stays_at_the_floor_otherwise():
    from types import SimpleNamespace as C

    from clic.rerank import Reranker

    rr = Reranker.__new__(Reranker)
    rr.relevance_k, rr.relevance_ema, rr.relevance_margin = 3, 0.9, 0.0
    rr.prompt_filter, rr.has_prompt, rr.min_prompt_score = True, True, 0.0
    rr._relevance_thr, rr._bar = None, 0.0
    ground = [C(s=x) for x in (0.02, -0.03, -0.05, -0.06, -0.08)]
    rr._update_bar(ground)
    assert rr._bar == 0.0  # the 3rd best is under 0: the floor decides
    clutter = [C(s=x) for x in (0.03, 0.02, 0.015, 0.01, -0.02)]
    for _ in range(60):
        rr._update_bar(clutter)
    assert abs(rr._bar - 0.015) < 1e-3  # the bar climbed to the clutter's 3rd best
    rr.relevance_k = 0
    rr._update_bar(clutter)
    assert rr._bar == 0.0
