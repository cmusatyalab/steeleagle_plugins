"""cliped following the verifier's prompt heartbeat, as box_rerank does."""
import dataclasses
import json

import numpy as np
import pytest
import uriel
from conftest import VERIFIER, BrightnessEncoder, input_id, make_frame, make_input

from cliped import main
from cliped.clip import Selector
from cliped.engine import PRIME_INTERVAL_S, Cliped
from cliped.prompt import PROMPT_TYPE, read_prompt
from cliped.send_queue import SendQueue
from cliped.similar import SimilarFilter
from test_learning import gray_frame, result_event

K = uriel.EventKind
WHITE, BLACK = np.array([1.0, 0.0]), np.array([0.0, 1.0])
# Each text prompt points t at a brightness, the other being the background
VECTORS = {"a white square": (WHITE, BLACK), "a black square": (BLACK, WHITE), "a gray square": (WHITE, BLACK)}


def prompt_event(text, engine=VERIFIER):
    return uriel.Event(kind=K.DERIVED, lost=0, id=input_id(1), engine=engine, flow="flow", outcome=0,
                       payload_type=PROMPT_TYPE, result=text.encode(), at_ns=0, offered_at_ns=None,
                       received_at_ns=None)


def heartbeat(description, kind="a square"):
    return prompt_event(json.dumps({"class": kind, "description": description}))


def selector_for(text):
    t, b = VECTORS[text]
    return Selector(t, b[None], k=4, margin=0.0, max_send=8, learn=True, similarity=0.99, good_similarity=0.99)


def make_cliped(prompt="", has_prompt=False, ignore_new=False, similar=None):
    switched = []

    def retarget(text):
        switched.append(text)
        return selector_for(text)

    encoder = BrightnessEncoder()
    cliped = Cliped(encoder, selector_for(prompt) if has_prompt else Selector(np.zeros(2), BLACK[None]),
                    SendQueue(max_size=8), prompt=prompt, verifier=VERIFIER, tile_size=200, stride=150,
                    has_prompt=has_prompt, retarget=None if ignore_new else retarget, ignore_new=ignore_new,
                    similar=similar)
    return cliped, switched


def white_frame(id):
    return make_frame(bright=(150, 0, 350, 200), id=id)


def test_without_a_prompt_frames_are_dropped_and_a_primer_tile_reaches_the_verifier():
    cliped, _ = make_cliped()
    assert cliped.on_input(make_input(1, white_frame(1)), now=0) == (None, b"")
    # One primer: the middle tile of the frame, never learned from
    primer, = cliped.queue._tiles
    assert primer.score == 0 and primer.embedding is None and primer.frame_id == 1
    x0, y0, x1, y1 = primer.box
    assert x0 <= 200 <= x1 and y0 <= 150 <= y1
    # Not another while it waits
    cliped.on_input(make_input(2, white_frame(2)), now=1)
    assert len(cliped.queue) == 1 and cliped.unprompted == 2
    # The verifier has it; not another before PRIME_INTERVAL_S
    cliped.queue.next(input_id(1), now=1)
    cliped.on_event(result_event(1, {"match": False}))
    assert len(cliped.queue) == 0 and not cliped.selector.backgrounds
    cliped.on_input(make_input(3, white_frame(3)), now=2)
    assert len(cliped.queue) == 0
    cliped.on_input(make_input(4, white_frame(4)), now=PRIME_INTERVAL_S + 1)
    assert len(cliped.queue) == 1


def test_the_first_heartbeat_sets_the_prompt_and_frames_are_searched():
    cliped, switched = make_cliped()
    cliped.on_input(make_input(1, white_frame(1)), now=0)  # a primer waits
    assert cliped.on_prompt_event(heartbeat("a white square", "a mannequin"))
    # The description is the prompt; the waiting primer is dropped
    assert switched == ["a white square"] and cliped.prompt == "a white square" and cliped.has_prompt
    assert cliped.generation == 1 and len(cliped.queue) == 0
    tile, result = cliped.on_input(make_input(2, white_frame(2)), now=1)
    assert tile is not None and tile.box == (150, 0, 350, 200) and tile.generation == 1 and result


def test_a_heartbeat_with_only_a_class_searches_for_the_class():
    cliped, switched = make_cliped()
    cliped.on_prompt_event(prompt_event(json.dumps({"class": "a white square", "description": None})))
    assert switched == ["a white square"]


def test_the_answer_to_a_primer_in_flight_is_not_learned():
    cliped, _ = make_cliped()
    cliped.on_input(make_input(1, white_frame(1)), now=0)
    cliped.queue.next(input_id(1), now=0)
    cliped.on_prompt_event(heartbeat("a gray square"))
    # In flight, so kept until the verifier has it; its answer teaches nothing
    assert len(cliped.queue) == 1
    cliped.on_event(result_event(1, {"match": False}))
    assert not cliped.selector.backgrounds and len(cliped.queue) == 0


def test_a_new_prompt_starts_learning_over_and_drops_the_waiting_tiles():
    cliped, switched = make_cliped("a gray square", has_prompt=True)
    cliped.on_input(make_input(1, gray_frame(1)), now=0)
    cliped.on_input(make_input(2, gray_frame(2)), now=0)
    cliped.queue.next(input_id(1), now=0)
    cliped.queue.next(input_id(2), now=0)
    cliped.on_event(result_event(1, {"match": False}))
    assert cliped.selector.backgrounds
    # The same prompt changes nothing
    assert cliped.on_prompt_event(heartbeat("a gray square"))
    assert switched == [] and cliped.generation == 0 and cliped.selector.backgrounds
    # Another: learning starts over, the waiting tiles go, the one in flight stays
    waiting = len(cliped.queue) - cliped.queue.in_flight
    assert waiting > 0
    assert cliped.on_prompt_event(heartbeat("a black square"))
    assert switched == ["a black square"] and cliped.generation == 1
    assert not cliped.selector.backgrounds and len(cliped.queue) == cliped.queue.in_flight == 1
    # The answer about the tile sent for the old prompt is not learned
    cliped.on_event(result_event(2, {"match": False}))
    assert not cliped.selector.backgrounds


def test_a_new_prompt_forgets_the_tiles_seen_for_the_old_one():
    similar = SimilarFilter()
    cliped, _ = make_cliped("a white square", has_prompt=True, similar=similar)
    cliped.on_input(make_input(1, white_frame(1)), now=0)
    assert similar._seen
    cliped.on_prompt_event(heartbeat("a black square"))
    assert not similar._seen


def test_with_ignore_new_the_prompt_is_kept():
    cliped, switched = make_cliped("a white square", has_prompt=True, ignore_new=True)
    assert cliped.on_prompt_event(heartbeat("a black square"))
    assert switched == [] and cliped.prompt == "a white square" and cliped.generation == 0


def test_only_the_verifiers_heartbeats_are_taken():
    cliped, switched = make_cliped()
    assert not cliped.on_prompt_event(dataclasses.replace(heartbeat("a white square"), engine="10.0.0.2.decoder"))
    assert not cliped.on_prompt_event(result_event(1, {"match": True}))
    assert switched == [] and not cliped.has_prompt


def test_a_heartbeat_that_is_not_utf8_is_ignored():
    cliped, switched = make_cliped()
    bad = dataclasses.replace(prompt_event("x"), result=b"\xff\xfe")
    assert cliped.on_prompt_event(bad) and switched == []


def test_read_prompt_is_shared_with_box_rerank():
    from cliped import rerank

    assert rerank.read_prompt is read_prompt


def test_args_prompt_and_image_are_optional_unless_ignore_new():
    args = main.parse_args(json.dumps({"verifier": "v"}))
    assert (args["prompt"], args["image"], args["ignore_new"]) == ("", "", False)
    assert main.parse_args(json.dumps({"prompt": "p", "verifier": "v", "ignore_new": True}))["ignore_new"] is True
    for bad in [{"verifier": "v", "ignore_new": True}, {"prompt": "p", "verifier": "v", "ignore_new": "yes"}]:
        with pytest.raises(main.ConfigError):
            main.parse_args(json.dumps(bad))
