"""box_rerank: the decision about each frame's boxes, what is queued for the
verifier, learning from its answers, and the args."""
import json

import numpy as np
import pytest
import uriel
from conftest import VERIFIER, input_id

from cliped import main
from cliped.clip import Selector
from cliped.engine import GENERIC_RESULT_TYPE, read_frame
from cliped.rerank import (
    DISCONNECTED,
    TOO_LATE,
    Reranker,
    max_input_size,
    pixel_box,
    square_around,
)
from cliped.send_queue import SendQueue
from cliped.verifier import enlarge
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.result import result_pb2

K = uriel.EventKind
DETECTOR = "10.0.0.1.detection_qnn"
WIDTH, HEIGHT = 400, 300
GREEN, BLUE, RED = (0, 200, 0), (0, 0, 200), (200, 0, 0)
# Where the test frames put the target (green) and the distractors
DOG, CHAIR, BAG = (40, 40, 80, 80), (200, 100, 240, 140), (300, 200, 340, 240)


class ColorEncoder:
    """Stands in for the image encoder: a crop's embedding is its mean color,
    normalized, so crops of differently colored objects are far apart."""

    size = 8

    def encode_tiles(self, tiles, batch_size=None):
        v = np.array([tile.reshape(-1, 3).mean(0) + 1e-3 for tile in tiles])
        return v / np.linalg.norm(v, axis=1, keepdims=True)


def frame(id=0, objects=((DOG, GREEN), (CHAIR, BLUE))):
    pixels = np.full((HEIGHT, WIDTH, 3), 10, dtype=np.uint8)
    for (x0, y0, x1, y1), color in objects:
        pixels[y0:y1, x0:x1] = color
    return frame_pb2.RawFrame(data=pixels.tobytes(), h_res=WIDTH, v_res=HEIGHT, channels=3, id=id,
                              camera_id=3).SerializeToString()


def detections(*boxes):
    """A DetectionResult of (box, score) or (box, score, class) in pixels."""
    result = result_pb2.DetectionResult()
    for box, score, *name in boxes:
        d = result.detections.add(class_name=name[0] if name else "robotic dog", score=score)
        x0, y0, x1, y1 = box
        d.bbox.x_min, d.bbox.y_min, d.bbox.x_max, d.bbox.y_max = x0 / WIDTH, y0 / HEIGHT, x1 / WIDTH, y1 / HEIGHT
    return result


def reported(result):
    """The (box, score, class) reported in the serialized DetectionResult
    `result`, or None."""
    if not result:
        return None
    (d,) = result_pb2.DetectionResult.FromString(result).detections
    return pixel_box(d.bbox, WIDTH, HEIGHT), round(d.score, 3), d.class_name


class Clock:
    def __init__(self):
        self.now_ns = 0

    def __call__(self):
        return self.now_ns


def make_reranker(**kwargs):
    selector = Selector(np.array([0.0, 1.0, 0.0]), np.array([[1.0, 0.0, 0.0]]), learn=True, similarity=0.85,
                        confirmations=2, good_similarity=0.9, good_confirmations=2)
    return Reranker(ColorEncoder(), selector, SendQueue(max_size=16), detector=DETECTOR, verifier=VERIFIER,
                    class_name="robotic dog", clock=Clock(), **{"cooldown_s": 5.0, **kwargs})


class Verifier:
    """Answers the reranker's queued crops, as the flow would: one at a time,
    best first; `truth(tile)` is the answer."""

    def __init__(self, reranker):
        self.reranker = reranker
        self.seq = 1000

    def answer_all(self, truth, now):
        answered = []
        while (tile := self.reranker.queue.next(input_id(self.seq), now)) is not None:
            match = truth(tile)
            event = uriel.Event(
                kind=K.RESULT, lost=0, id=input_id(self.seq), engine=VERIFIER, flow="flow", outcome=0,
                payload_type=GENERIC_RESULT_TYPE,
                result=result_pb2.GenericResult(payload=json.dumps({"match": match}).encode()).SerializeToString(),
                at_ns=0, offered_at_ns=None, received_at_ns=None)
            self.reranker.on_queue_event(event, now=now)
            answered.append((tile, match))
            self.seq += 1
        return answered


def is_dog(tile):
    # The crop's box is a square around the detection's: the dog's is near
    # the top left
    return tile.box[0] < 100


def test_with_nothing_learned_it_reports_the_most_confident_box_renamed_and_queues_every_box():
    r = make_reranker()
    result = r.decide(frame(), detections((CHAIR, 0.8, "chair"), (DOG, 0.6, "Robotic Dog")), now=0)
    # Only the class's boxes are candidates
    assert reported(result) == (DOG, 0.6, "robotic dog")
    result = r.decide(frame(1), detections((CHAIR, 0.8), (DOG, 0.6)), now=10)
    assert reported(result) == (CHAIR, 0.8, "robotic dog")  # the false positive this engine is for
    queued = sorted(r.queue, key=lambda t: t.priority, reverse=True)
    # The dog's crop from frame 0 is still queued, so the dog of frame 1 is
    # not queued again; the chair is, the box reported going first
    assert [(t.frame_id, t.must_send) for t in queued] == [(1, True), (0, True)]


def test_unverified_boxes_under_the_confidence_are_never_reported():
    r = make_reranker(confidence=0.5)
    assert r.decide(frame(), detections((DOG, 0.3), (CHAIR, 0.45)), now=0) == b""
    # ... but still go to the verifier, to learn from
    assert len(r.queue) == 2


def test_two_nos_veto_a_distractor_and_the_target_is_reported_instead():
    r = make_reranker()
    verifier = Verifier(r)
    for i in range(2):
        r.decide(frame(i), detections((CHAIR, 0.9), (DOG, 0.6)), now=i * 10)
        verifier.answer_all(is_dog, now=i * 10)
    assert r.vetoing == 1
    result = r.decide(frame(5), detections((CHAIR, 0.9), (DOG, 0.6)), now=30)
    assert reported(result) == (DOG, 0.6, "robotic dog")
    # With the dog gone, the chair alone is no detection, however confident
    assert r.decide(frame(6, objects=((CHAIR, BLUE),)), detections((CHAIR, 0.95)), now=31) == b""


def test_one_no_does_not_veto():
    r = make_reranker()
    r.decide(frame(0), detections((CHAIR, 0.9)), now=0)
    Verifier(r).answer_all(is_dog, now=0)
    assert r.vetoing == 0
    assert reported(r.decide(frame(1, objects=((CHAIR, BLUE),)), detections((CHAIR, 0.9)), now=1))[0] == CHAIR


def test_a_confirmed_target_is_reported_at_any_confidence_ahead_of_unverified_boxes():
    r = make_reranker(confidence=0.5)
    verifier = Verifier(r)
    for i in range(2):
        r.decide(frame(i, objects=((DOG, GREEN),)), detections((DOG, 0.7)), now=i * 10)
        verifier.answer_all(is_dog, now=i * 10)
    assert len(r.selector.confirmed_good) == 1
    # A bag the verifier never saw, more confident than the dog
    objects = ((DOG, GREEN), (BAG, RED))
    result = r.decide(frame(5, objects=objects), detections((BAG, 0.95), (DOG, 0.2)), now=30)
    assert reported(result) == (DOG, 0.2, "robotic dog")
    # The confirmed dog is not queued again; the bag is
    assert [t.box for t in r.queue] == [square_around(BAG, r.verify_context, r.min_verify_side, WIDTH, HEIGHT)]


def test_require_verified_reports_nothing_until_the_verifier_confirms():
    r = make_reranker(require_verified=True)
    verifier = Verifier(r)
    assert r.decide(frame(0, objects=((DOG, GREEN),)), detections((DOG, 0.9)), now=0) == b""
    verifier.answer_all(is_dog, now=0)
    assert r.decide(frame(1, objects=((DOG, GREEN),)), detections((DOG, 0.9)), now=10) == b""
    verifier.answer_all(is_dog, now=10)
    assert reported(r.decide(frame(2, objects=((DOG, GREEN),)), detections((DOG, 0.9)), now=20))[0] == DOG


def test_a_crop_like_one_answered_waits_out_the_cooldown_before_it_is_asked_about_again():
    r = make_reranker(cooldown_s=5.0)
    verifier = Verifier(r)
    r.decide(frame(0), detections((CHAIR, 0.9)), now=0)
    assert len(verifier.answer_all(is_dog, now=0)) == 1
    r.decide(frame(1), detections((CHAIR, 0.9)), now=3)
    assert len(r.queue) == 0
    r.decide(frame(2), detections((CHAIR, 0.9)), now=6)
    assert len(r.queue) == 1


def test_a_veto_drops_the_queued_crops_it_settles():
    r = make_reranker()
    verifier = Verifier(r)
    r.decide(frame(0), detections((CHAIR, 0.9)), now=0)
    verifier.answer_all(is_dog, now=0)
    r.decide(frame(1), detections((CHAIR, 0.9)), now=10)
    # A second chair crop waits behind the one about to be answered: a crop
    # queued before the veto, as if it had come before the cooldown check
    tile = next(iter(r.queue))
    r.queue.push(type(tile)(score=0.5, payload=tile.payload, frame_id=2, box=tile.box, embedding=tile.embedding))
    first = r.queue.next(input_id(1), 10)
    assert first.frame_id == 1
    event = uriel.Event(
        kind=K.RESULT, lost=0, id=input_id(1), engine=VERIFIER, flow="flow", outcome=0,
        payload_type=GENERIC_RESULT_TYPE,
        result=result_pb2.GenericResult(payload=json.dumps({"match": False}).encode()).SerializeToString(),
        at_ns=0, offered_at_ns=None, received_at_ns=None)
    r.on_queue_event(event, now=10)
    assert r.vetoing == 1 and len(r.queue) == 0


def test_the_verifier_sees_a_larger_square_with_the_box_in_red():
    r = make_reranker(verify_context=2.0, min_verify_side=96, max_verify_side=256)
    r.decide(frame(7), detections((DOG, 0.9)), now=0)
    (tile,) = r.queue
    out, rgb = read_frame(tile.payload)
    assert out.id == 7 and out.camera_id == 3
    # 2 x 40 = 80, raised to 96, centered on the dog and shifted into the frame
    assert tile.box == (12, 12, 108, 108) and rgb.shape == (96, 96, 3)
    x0, y0 = DOG[0] - 12, DOG[1] - 12
    assert tuple(rgb[y0, x0 + 10]) == (255, 0, 0)  # the box's top edge
    assert tuple(rgb[y0 + 20, x0 + 20]) == GREEN  # inside it, untouched


def test_a_large_crop_is_scaled_down_to_max_verify_side():
    r = make_reranker(max_verify_side=64)
    big = (100, 50, 300, 250)
    r.decide(frame(0, objects=((big, GREEN),)), detections((big, 0.9)), now=0)
    (tile,) = r.queue
    assert read_frame(tile.payload)[1].shape == (64, 64, 3)
    assert len(tile.payload) < max_input_size(64)


def test_square_around_stays_in_the_frame():
    assert square_around((0, 0, 10, 10), 2.0, 0, 400, 300) == (0, 0, 20, 20)
    assert square_around((390, 290, 400, 300), 2.0, 0, 400, 300) == (380, 280, 400, 300)
    # Larger than the frame: shrunk to it, still centered
    assert square_around((0, 0, 400, 300), 2.0, 0, 400, 300) == (50, 0, 350, 300)


def test_only_the_best_max_candidates_are_embedded():
    r = make_reranker(max_candidates=1)
    r.decide(frame(), detections((CHAIR, 0.5), (DOG, 0.9)), now=0)
    assert [t.frame_id for t in r.queue] == [0] and len(r.queue) == 1


def test_detector_outcomes_and_failures():
    r = make_reranker(timeout_ns=100)
    event = uriel.Event(kind=K.RESULT, lost=0, id=input_id(1), engine=DETECTOR, flow="flow", outcome=0,
                        payload_type="steeleagle_protocol.v1.messages.result.DetectionResult",
                        result=detections((DOG, 0.9)).SerializeToString(), at_ns=0, offered_at_ns=None,
                        received_at_ns=None)
    r.on_detector_event(event)
    outcome = r.outcome(input_id(1), r.deadline())
    assert reported(r.decide(frame(), outcome, now=0))[0] == DOG
    # Not in yet: wait, until the deadline
    deadline = r.deadline()
    assert r.outcome(input_id(2), deadline) is None
    r.clock.now_ns = deadline
    assert r.outcome(input_id(2), deadline) == TOO_LATE
    assert r.decide(frame(), TOO_LATE) == b""
    r.on_detector_event(uriel.Event(kind=K.DISCONNECTED, lost=0, id=input_id(0), engine=DETECTOR, flow="flow",
                                    outcome=0, payload_type="", result=b"", at_ns=0, offered_at_ns=None,
                                    received_at_ns=None))
    assert r.outcome(input_id(3), r.deadline()) == DISCONNECTED


def test_args_are_defaulted_and_checked():
    args = main.parse_rerank_args(json.dumps({"prompt": "a robot dog", "detector": "yolo", "verifier": "v",
                                              "class": "robotic dog"}))
    assert args["classes"] == ["robotic dog"] and args["confidence"] == 0.5 and args["timeout"] == 1000
    for bad in ({"class": ""}, {"confidence": 1.5}, {"novelty": 0}, {"classes": "dog"},
                {"min_verify_side": 300, "max_verify_side": 256}, {"max_candidates": 0}):
        with pytest.raises(main.ConfigError):
            main.parse_rerank_args(json.dumps({"prompt": "p", "detector": "yolo", "verifier": "v",
                                               "class": "robotic dog", **bad}))
    with pytest.raises(main.ConfigError):
        main.parse_rerank_args(json.dumps({"detector": "yolo", "verifier": "v", "class": "c"}))


def test_the_verifier_enlarges_small_frames_only():
    small = np.zeros((48, 64, 3), dtype=np.uint8)
    assert enlarge(small, 96).shape == (96, 128, 3)
    assert enlarge(small, 0) is small and enlarge(small, 32) is small
    args = main.parse_verifier_args(json.dumps({"prompt": "a robot dog", "question": "box", "min_side": 224}))
    assert args["min_side"] == 224
