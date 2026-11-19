"""defer_similar: near-copies of a tile sent before go behind the new ones.

The tiles in data/similar/ are ones the verifier got from the sandr_test
flight (prompt "signs of human activity", 2026-11-19): a bike in
consecutive picked frames (2673, 2679, 2685) and after it had moved
furthest in that pass (2730), one person from two angles (755, 2958), and
plain grass (4127)."""
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from conftest import VERIFIER, BrightnessEncoder, make_input
from steeleagle_protocol.v1.messages.frame import frame_pb2

from cliped import main
from cliped.clip import Selector
from cliped.engine import Cliped
from cliped.send_queue import SendQueue, Tile
from cliped.similar import MIN_MATCHES, SimilarFilter

DATA = Path(__file__).parent / "data" / "similar"


def tile(name):
    return cv2.cvtColor(cv2.imread(str(DATA / f"{name}.jpeg")), cv2.COLOR_BGR2RGB)


def test_a_slightly_shifted_copy_is_not_new():
    f = SimilarFilter()
    assert f.is_new(tile("bike_2673_0"), None, 2673)
    assert not f.is_new(tile("bike_2679_0"), None, 2679)
    assert not f.is_new(tile("bike_2685_0"), None, 2685)
    assert f.similar == 2


def test_other_views_and_other_things_are_new():
    f = SimilarFilter()
    for name in ("bike_2673_0", "person_755", "person_2958", "grass_4127"):
        assert f.is_new(tile(name), None, 0), name
    assert f.similar == 0


def test_the_copies_match_far_more_than_other_views():
    f = SimilarFilter()
    bike = f.features(tile("bike_2673_0"))
    assert f.matches(bike, f.features(tile("bike_2679_0"))) >= MIN_MATCHES
    assert f.matches(f.features(tile("person_755")), f.features(tile("person_2958"))) < MIN_MATCHES
    assert f.matches(bike, f.features(tile("grass_4127"))) < MIN_MATCHES


def test_tiles_unlike_by_clip_are_not_compared():
    f = SimilarFilter(gate=0.9)
    assert f.is_new(tile("bike_2673_0"), np.array([1.0, 0.0]), 1)
    # The same pixels, but embeddings far apart: never compared, so new
    assert f.is_new(tile("bike_2673_0"), np.array([0.0, 1.0]), 2)
    assert not f.is_new(tile("bike_2679_0"), np.array([1.0, 0.0]), 3)


def test_a_similar_tile_goes_behind_every_new_one_but_stays_queued():
    queue = SendQueue(max_size=8)
    copy = Tile(score=0.9, payload=b"", frame_id=1, box=(0, 0, 1, 1), must_send=True, similar=True)
    new = Tile(score=0.1, payload=b"", frame_id=2, box=(0, 0, 1, 1))
    queue.push(copy)
    queue.push(new)
    assert queue.best() is new
    queue.sent(new, "carrier", 0)
    assert queue.best() is copy


def frame_with(name, id):
    """A black 400x300 frame with the tile `name`, brightened to stand out
    to the brightness encoder, at (150, 0, 350, 200)."""
    pixels = np.zeros((300, 400, 3), dtype=np.uint8)
    pixels[0:200, 150:350] = np.clip(tile(name).astype(np.int16) + 120, 0, 255)
    return frame_pb2.RawFrame(data=pixels.tobytes(), h_res=400, v_res=300, channels=3, id=id).SerializeToString()


@pytest.mark.parametrize("on", [False, True])
def test_the_engine_marks_picked_tiles_like_one_sent_before(on):
    encoder = BrightnessEncoder()
    cliped = Cliped(encoder, Selector(encoder.t, encoder.b, margin=0.25, max_send=1), SendQueue(max_size=8),
                    prompt="p", verifier=VERIFIER, tile_size=200, stride=150,
                    similar=SimilarFilter() if on else None)
    tiles = [cliped.on_input(make_input(i, frame_with(name, i)), now=i)[0]
             for i, name in enumerate(["bike_2673_0", "bike_2679_0", "person_755"])]
    assert [t.box for t in tiles] == [(150, 0, 350, 200)] * 3
    assert [t.similar for t in tiles] == ([False, True, False] if on else [False] * 3)
    assert len(cliped.queue) == 3  # still sent, eventually


def test_defer_similar_defaults_to_off_and_validates():
    args = main.parse_args(json.dumps({"prompt": "p", "verifier": "v"}))
    assert (args["defer_similar"], args["defer_similar_matches"]) == (False, MIN_MATCHES)
    args = main.parse_args(json.dumps({"prompt": "p", "verifier": "v", "defer_similar": True,
                                       "defer_similar_matches": 60}))
    assert (args["defer_similar"], args["defer_similar_matches"]) == (True, 60)
    for bad in [{"defer_similar": "yes"}, {"defer_similar_matches": 0}, {"defer_similar_matches": 2.5}]:
        with pytest.raises(main.ConfigError):
            main.parse_args(json.dumps({"prompt": "p", "verifier": "v", **bad}))
