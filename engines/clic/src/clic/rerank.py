"""box_rerank: picks which of a local detector's boxes, if any, is the target,
by what a remote verifier said about boxes that looked like it before.

The engine gets the same camera frames as a detection engine on this daemon
(e.g. detection-qnn running YOLO) and subscribes to its events. For each
frame it waits for the detector's DetectionResult, crops the candidates (the
detections of `classes` at or above `input_confidence`, less any that duplicate
a better one (see `dedupe_overlap`), best `max_candidates` by confidence) with a little
context around each, and embeds each crop with the CLIP image encoder. What
the verifier said about earlier crops (clic.clip.Selector's learning, the
same as the edge filter's) gives each candidate a verdict:

- -1, vetoed: like a crop the verifier said no to, again and again (a chair,
  a bag that the detector takes for the target). Never reported.
- +1, confirmed: like a crop the verifier said yes to, again and again.
  Reported ahead of any unconfirmed candidate.
- 0, unverified: reported only if its confidence is at least `confidence`
  (and never with `require_verified`).

The frame is dropped, its result a DetectionResult holding the one candidate
reported, named `class_name`, or no result if no candidate is eligible: a
false positive costs a tracker more than a missed frame.

With `prompt_filter`, cliped's relevance floor applies too: an unverified
candidate whose s (how much more like the prompt than the backgrounds) is not
above `min_prompt_score` is neither queued for the verifier nor reported, as
cliped sends no tile with s at or below 0. A confirmed one is exempt: the
verifier said yes to crops like it.

Learning runs off the frame path. Each unverified candidate is queued for the
verifier (ordered by the detector's confidence, or with `queue_by` "prompt" by s,
as cliped orders its tiles), unless it is like a crop already queued, or one the verifier
answered in the last `cooldown_s` (so that a distractor seen in every frame
is asked about again only after a while, to confirm a no or a yes); the one
reported goes first. Its payload is a square crop with more context than the
embedded crop and the box drawn in red, so the verifier can tell which object
is meant. The engine's reintegration producer delivers the queue, one crop at
a time, as cliped's does, and each RESULT of the verifier is learned. The
frames themselves never wait on the verifier.
"""
import csv
import logging
import math
import time
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass

import cv2
import numpy as np
import uriel
from google.protobuf.message import DecodeError

from clic.clip import CONFIRMED, CONFIRMED_GOOD, DROPPED_GOOD
from clic.engine import (
    OUTPUT_HEADROOM,
    POLL_INTERVAL_S,
    RAW_FRAME_TYPE,
    REINTEGRATION,
    RGB_CHANNELS,
    UNSURE,
    FrameError,
    Reintegration,
    producer_name,
    read_frame,
    read_verdict,
    tile_frame,
)
from clic.send_queue import Tile
from clic.sent_log import log_path
from clic.prompt import PROMPT_TYPE, heartbeat_prompt, is_heartbeat, read_prompt, type_name  # noqa: F401
from steeleagle_protocol.v1.messages.result import result_pb2

logger = logging.getLogger("clic.rerank")

DETECTION_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/DetectionResult"
# The largest result reported: one detection
MAX_RESULT_SIZE = 64 << 10
# The most detector outcomes kept for frames that have not arrived
MAX_OUTCOMES = 1024
SUMMARY_INTERVAL_NS = 30 * 1_000_000_000
BOX_COLOR = (255, 0, 0)

VETOED, UNVERIFIED, CONFIRMED_TARGET = -1, 0, 1

# Why a frame has no detection
SKIPPED = "detector skipped"
TIMED_OUT = "detector timed out"
DISCONNECTED = "detector disconnected"
UNDECODABLE = "undecodable result"
TOO_LATE = "result too late"
_FAILED = {uriel.EventKind.SKIPPED: SKIPPED, uriel.EventKind.TIMED_OUT: TIMED_OUT}


def normalize_class(name):
    return name.strip().casefold()


def max_input_size(max_verify_side):
    """The largest crop the reintegration producer sends."""
    return max_verify_side * max_verify_side * RGB_CHANNELS + OUTPUT_HEADROOM


def parse_detections(payload_type, data):
    """The DetectionResult serialized in `data`, or None if it is not one. A
    detector that drops a frame with no result found nothing: an empty
    DetectionResult."""
    if payload_type and type_name(payload_type) != DETECTION_RESULT_TYPE:
        return None
    try:
        return result_pb2.DetectionResult.FromString(data)
    except DecodeError:
        return None


def pixel_box(bbox, width, height):
    """The normalized BoundingBox `bbox` in a `width` x `height` frame, as
    (left, top, right, bottom) pixels, grown to whole pixels and clipped to
    the frame; None if nothing of it is in the frame."""
    left = max(0, math.floor(min(bbox.x_min, bbox.x_max) * width))
    top = max(0, math.floor(min(bbox.y_min, bbox.y_max) * height))
    right = min(width, math.ceil(max(bbox.x_min, bbox.x_max) * width))
    bottom = min(height, math.ceil(max(bbox.y_min, bbox.y_max) * height))
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


# What orders box_rerank's queued crops for the verifier: the detector's confidence, or s
QUEUE_BY_CONFIDENCE = "confidence"
QUEUE_BY_PROMPT = "prompt"
QUEUE_BY = (QUEUE_BY_CONFIDENCE, QUEUE_BY_PROMPT)

# How the unverified candidates of a frame are ranked: by confidence (plus
# prompt_weight times s), or by their CLIP prompt score s against each other
RANK_CONFIDENCE = "confidence"
RANK_PROMPT = "prompt"
RANKS = (RANK_CONFIDENCE, RANK_PROMPT)


def square_around(box, scale, min_side, width, height):
    """The square (left, top, right, bottom) centered on `box`, its side
    `scale` times the box's longer side and at least `min_side`, shifted to
    lie in the `width` x `height` frame (and shrunk to it if larger)."""
    left, top, right, bottom = box
    side = max(max(right - left, bottom - top) * scale, min_side)
    side = int(round(min(side, width, height)))
    x0 = round((left + right - side) / 2)
    y0 = round((top + bottom - side) / 2)
    x0 = min(max(x0, 0), width - side)
    y0 = min(max(y0, 0), height - side)
    return x0, y0, x0 + side, y0 + side


@dataclass
class Candidate:
    detection: object  # its Detection
    box: tuple  # in pixels
    embedding: np.ndarray
    s: float  # how much more like the prompt than a background
    verdict: int
    margin: float = float("nan")  # how much more like the prompt than the class, with a gate
    jumped: bool = False  # its score jumped over every crop like it asked about (score_jump)

    @property
    def score(self):
        return self.detection.score


# How many crops queued the score jump compares a crop with
ASKED_REMEMBERED = 512


class CandidateLog:
    """One CSV row per candidate of every frame decided, so that what was
    reported can be scored against labeled boxes: the frame's id and capture
    timestamp, the candidate's number in the frame (best confidence first),
    its class, confidence, box in pixels, prompt score s, verdict (-1 vetoed,
    0 unverified, 1 confirmed), whether it was reported and whether it was
    queued for the verifier, and the milliseconds the frame took.
    Line-buffered."""

    HEADER = ("frame_id", "timestamp_ns", "candidate", "class", "score", "left", "top", "right", "bottom",
              "frame_width", "frame_height", "s", "verdict", "reported", "queued", "ms", "margin")

    def __init__(self, path):
        self.path = log_path(path)
        self._file = open(self.path, "w", newline="", buffering=1)
        self._writer = csv.writer(self._file)
        self._writer.writerow(self.HEADER)

    def write(self, frame, width, height, candidates, reported, queued, ms):
        stamp = frame.timestamp.ToNanoseconds()
        for i, c in enumerate(candidates):
            self._writer.writerow((frame.id, stamp, i, c.detection.class_name, f"{c.score:.4f}", *c.box, width,
                                   height, f"{c.s:.4f}", c.verdict, int(i == reported), int(i in queued),
                                   f"{ms:.1f}", "" if c.margin != c.margin else f"{c.margin:.4f}"))

    def close(self):
        self._file.close()


class AnswerLog:
    """One CSV row per verifier answer about a crop, so that what reached
    the verifier, and what it passed on, can be scored against labeled
    boxes: the crop's frame id and number in the frame (as in the candidate
    log), the square the verifier saw, in pixels of the frame, its
    confidence, and the answer (yes, no or unsure). Line-buffered."""

    HEADER = ("frame_id", "candidate", "left", "top", "right", "bottom", "score", "answer", "time_ns")

    def __init__(self, path):
        self.path = log_path(path)
        self._file = open(self.path, "w", newline="", buffering=1)
        self._writer = csv.writer(self._file)
        self._writer.writerow(self.HEADER)

    def write(self, tile, match):
        answer = "unsure" if match == UNSURE else "yes" if match else "no"
        self._writer.writerow((tile.frame_id, tile.index, *tile.box, f"{tile.score:.4f}", answer, time.time_ns()))

    def close(self):
        self._file.close()


class Reranker:
    """Decides each frame from the detector's result and what the verifier
    said. `encoder` embeds crops, `selector` (clip.Selector, with learning)
    holds what the verifier said, and `queue` (send_queue.SendQueue) the
    crops waiting for it; see the module for the rest."""

    def __init__(self, encoder, selector, queue, *, detector, verifier, class_name, classes=(), confidence=0.5,
                 input_confidence=0.0, dedupe_overlap=0.0, dedupe_max_area_ratio=4.0, max_candidates=8, context=1.25, verify_context=2.0, min_verify_side=96, max_verify_side=256,
                 novelty=0.92, cooldown_s=5.0, require_verified=False, prompt_weight=0.0, rank=RANK_PROMPT,
                 gate=None, prompt=None, has_prompt=True, retarget=None, ignore_new=False, gate_class=None,
                 timeout_ns=1_000_000_000, similar=None, visual=None, visual_guard=False, propagate_s=0.0,
                 score_jump=0.0, score_jump_floor=0.0, prompt_filter=False, min_prompt_score=0.0,
                 queue_by=QUEUE_BY_CONFIDENCE, lift_vetoes=True, repeats=None,
                 relevance_k=0, relevance_ema=0.9, relevance_margin=0.0,
                 clock=time.monotonic_ns, tile_log=None, candidate_log=None, answer_log=None,
                 summary_interval_ns=SUMMARY_INTERVAL_NS):
        self.encoder = encoder
        self.selector = selector
        self.queue = queue
        self.detector = detector
        self.verifier = verifier
        self.class_name = class_name
        self.classes = {normalize_class(c) for c in (classes or [class_name])}
        self.confidence = confidence
        self.max_candidates = max_candidates
        # Detections below this are dropped before anything else: never candidates, embedded or asked about
        self.input_confidence = input_confidence
        # A box at least `dedupe_overlap` inside a better-scoring candidate of the frame, the larger of the two at
        # most `dedupe_max_area_ratio` times the other's area, is a duplicate of it (a part, or the same object
        # framed a little differently) and dropped before max_candidates is applied; a much larger box (a scene
        # around small objects) never removes a small one, nor the other way round. 0 turns it off
        self.dedupe_overlap = dedupe_overlap
        # cliped's relevance floor on s for unverified candidates (queued or reported); off without a prompt
        self.prompt_filter = prompt_filter
        self.min_prompt_score = min_prompt_score
        # With the prompt filter, cliped's adaptive bar too (Selector.select): each frame, the relevance_k-th best
        # candidate's s is smoothed by an EMA (relevance_ema, the old bar's weight), and an unverified candidate
        # must also beat that plus relevance_margin times the frame's std of s; 0 is off. In a stretch of frames
        # full of clutter that scores (a stump in a pond, glinting water) the bar climbs over it
        self.relevance_k = relevance_k
        self.relevance_ema = relevance_ema
        self.relevance_margin = relevance_margin
        self._relevance_thr = None
        self._bar = min_prompt_score
        # What orders the queued crops: the detector's confidence, or s (as cliped orders its tiles)
        if queue_by not in QUEUE_BY:
            raise ValueError(f"queue_by must be one of {', '.join(QUEUE_BY)}, not {queue_by!r}")
        self.queue_by = queue_by
        self.dedupe_max_area_ratio = dedupe_max_area_ratio
        self.context = context
        self.verify_context = verify_context
        self.min_verify_side = min_verify_side
        self.max_verify_side = max_verify_side
        self.novelty = novelty
        self.cooldown_s = cooldown_s
        self.require_verified = require_verified
        self.prompt_weight = prompt_weight
        if rank not in RANKS:
            raise ValueError(f"rank must be one of {RANKS}, not {rank!r}")
        self.rank = rank
        # An instance_gate.InstanceGate, or None
        self.gate = gate
        # The text prompt (None for an image or none), whether there is a
        # prompt at all (without one, boxes are ranked by confidence), and
        # retarget(text, class_prompt), which gives a new selector and gate
        # for the text prompt `text` (the gate over `class_prompt`, or this
        # engine's own for None), for the verifier's prompt (on_prompt)
        self.prompt = prompt
        self.class_prompt = None
        # The class prompt the gate is over when the verifier sends none (with
        # a gate), so a heartbeat bringing that same class changes nothing
        self.gate_class = " ".join(gate_class.split()) if gate_class else None
        self.has_prompt = has_prompt
        self.retarget = retarget
        # Whether to keep the configured prompt whatever the verifier's is
        self.ignore_new = ignore_new
        self.generation = 0
        self.timeout_ns = timeout_ns
        self.clock = clock
        self.tile_log = tile_log
        self.candidate_log = candidate_log
        # Score jump: a crop whose detector score is at least score_jump times
        # that of every crop like it asked about before (at least
        # score_jump_floor) is asked about although it looks like a repeat,
        # and no veto stops it: something new may be at a place answered
        # before (a person walking into a spot said no to while empty).
        # (embedding, detector score) of each crop queued; 0 is off
        self.score_jump = score_jump
        self.score_jump_floor = score_jump_floor
        self._asked = deque(maxlen=ASKED_REMEMBERED)
        self.answer_log = answer_log
        self.summary_interval_ns = summary_interval_ns
        # Per input id: the detector's DetectionResult, or why it has none
        self._outcomes = OrderedDict()
        self._disconnected = False
        # (embedding, time.monotonic()) of each crop the verifier answered
        # within the cooldown
        self._answered = []
        self._counts = Counter()
        self._summary_at_ns = None
        # Seconds spent in each step of decide() since the last summary, and crops embedded
        self._time = Counter()
        self.answers = Counter()
        # As in cliped (engine.Cliped): a similar.SimilarFilter that tells the
        # crops like one queued before, which go behind the new ones, or None
        # (defer_similar off); and one whose ORB keypoints the visual guard
        # and propagation use, or None for neither
        self.similar = similar
        self.visual = visual
        self.visual_guard = visual_guard and visual is not None
        # With the visual guard, whether a confirmed background's veto is lifted for a crop whose keypoints don't
        # match it (ORB on the full-size crop, up to ~30 ms a frame); off, only the guarding of no's stays
        self.lift_vetoes = lift_vetoes
        self.propagate_s = propagate_s if visual is not None else 0.0
        # clic_smart_search's repeat check: a similar.SentMemory of what was sent, or None. A crop of an object
        # sent before (within its window, whatever the answer) is held back unless its score jumped over the
        # last send's (score_jump, at least score_jump_floor)
        self.repeats = repeats
        # The crops queued in the last propagate_s: (time.monotonic(), tile)
        self._recent = deque()
        # The keypoints of the crops the verifier said yes to
        self._good_features = deque(maxlen=max(1, getattr(selector, "max_learned", 32)))

    # The detector

    def on_detector_event(self, event):
        """Takes an event of the detector."""
        if event.engine != self.detector:
            return
        kind = event.kind
        if kind == uriel.EventKind.DISCONNECTED:
            if not self._disconnected:
                logger.info("%s disconnected", self.detector)
            self._disconnected = True
            return
        if kind == uriel.EventKind.LOST:
            logger.warning("%d events of %s were lost", event.lost, self.detector)
            return
        if kind not in (uriel.EventKind.RESULT, *_FAILED):
            return
        if self._disconnected:
            logger.info("%s is reachable again", self.detector)
            self._disconnected = False
        if kind == uriel.EventKind.RESULT:
            outcome = parse_detections(event.payload_type, event.result)
            if outcome is None:
                logger.warning("input %d: %s sent a %r, not a DetectionResult", event.id.seq, self.detector,
                               event.payload_type)
                outcome = UNDECODABLE
        else:
            outcome = _FAILED[kind]
        self._outcomes[event.id] = outcome
        while len(self._outcomes) > MAX_OUTCOMES:
            self._outcomes.popitem(last=False)

    def deadline(self):
        """When a frame that reaches the engine now is given up on."""
        return self.clock() + self.timeout_ns

    def outcome(self, id, deadline):
        """The detector's DetectionResult for the input `id`, or why it has
        none; None while it is still worth waiting for, up to `deadline`."""
        outcome = self._outcomes.pop(id, None)
        if outcome is not None:
            return outcome
        if self._disconnected:
            return DISCONNECTED
        if self.clock() >= deadline:
            return TOO_LATE
        return None

    # The verifier

    def on_queue_event(self, event, now=None):
        """Takes an event about a reintegration input, at any engine, and
        learns from the verifier's answer."""
        if self.on_prompt_event(event):
            return
        tile = self.queue.on_event(event, self.verifier)
        if tile is None:
            return
        if self.answer_log is not None:
            answered = read_verdict(event)
            if answered is not None:
                self.answer_log.write(tile, answered)
        if tile.embedding is None:
            return
        if tile.generation != self.generation:
            logger.info("verifier: an answer about the box of frame %d, sent for an earlier prompt; not learned",
                        tile.frame_id)
            return
        match = read_verdict(event)
        if match is None:
            return
        tile.answered = True
        now = time.monotonic() if now is None else now
        # Asked about, so a crop like it isn't asked about again until the
        # cooldown, whatever the answer
        self._answered.append((tile.embedding, now))
        if match == UNSURE:
            self.answers["unsure"] += 1
            logger.info("verifier: unsure about the box of frame %d (score %.2f); not learned",
                        tile.frame_id, tile.score)
            return
        self.answers["yes" if match else "no"] += 1
        outcome = self._learn_one(tile, match)
        changed = outcome in (CONFIRMED_GOOD, CONFIRMED, DROPPED_GOOD)
        propagated = []
        for other, n in self._like(tile, now):
            also = self._learn_one(other, match, counted=False)
            changed |= also in (CONFIRMED_GOOD, CONFIRMED, DROPPED_GOOD)
            propagated.append(f"{other.frame_id} ({n} keypoints): {also}")
        message = "verifier: %s for the box of frame %d (score %.2f): %s (%d good, %d confirmed; %d no's, %d vetoing)"
        args = ["yes" if match else "no", tile.frame_id, tile.score, outcome, len(self.selector.good),
                len(self.selector.confirmed_good), len(self.selector.backgrounds), self.vetoing]
        if propagated:
            message += "; also learned for the crops like it of frames %s"
            args.append(", ".join(propagated))
        if changed:
            dropped = self.queue.rescore(self._still_unverified)
            message += "; dropped %d queued crops it settled (%d left)"
            args += [len(dropped), len(self.queue)]
        logger.info(message, *args)

    def _learn_one(self, tile, match, counted=True):
        """Learns the answer `match` for `tile` (the selector and the gate):
        with the visual guard, a no is guarded only if the crop's keypoints
        match a crop said yes to, in place of the cosine guard. An answer
        copied to it (`counted` off) adds no yes or no to a crop already
        learned (Selector.learn), nor an answer to the gate."""
        guarded = None
        if not match and self.visual_guard and tile.features is not None:
            guarded = any(self.visual.matches(tile.features, g) >= self.visual.min_matches
                          for g in self._good_features)
        outcome = self.selector.learn(tile.embedding, match, guarded=guarded, counted=counted,
                                      features=tile.features if self.visual_guard else None)
        if self.gate is not None and counted:
            self.gate.learn(tile.embedding, match)
        if match and tile.features is not None:
            self._good_features.append(tile.features)
        return outcome

    def _like(self, tile, now):
        """The crops queued in the last propagate_s, for the current prompt
        and not answered, that are visually `tile`, with how many keypoints
        match (propagation, as in cliped)."""
        if not self.propagate_s or tile.features is None:
            return []
        while self._recent and now - self._recent[0][0] > self.propagate_s:
            self._recent.popleft()
        like = []
        for _, other in self._recent:
            if (other is tile or other.answered or other.generation != self.generation
                    or other.features is None or other.embedding is None):
                continue
            n = self.visual.same(tile.features, other.features, tile.embedding, other.embedding)
            if n >= self.visual.min_matches:
                like.append((other, n))
        return like

    def on_prompt_event(self, event):
        """Takes an event of the verifier: switches to the prompt its
        heartbeat (a DERIVED event of type PROMPT_TYPE) holds. Returns whether
        the event was a heartbeat. With ignore_new, a heartbeat changes
        nothing."""
        if not is_heartbeat(event, self.verifier):
            return False
        if self.ignore_new:
            return True
        prompt = heartbeat_prompt(event)
        if prompt is not None:
            self.on_prompt(*prompt)
        return True

    def on_prompt(self, text, class_prompt=None):
        """Switches to the text prompt `text`, the verifier's, with its class
        prompt `class_prompt` (for the gate; None for this engine's own), if
        either isn't the current one, and starts learning over: what the
        verifier said about the old prompt says nothing about the new one.
        The crops waiting for the verifier are dropped; answers about the
        ones in flight are not learned from. Returns whether it switched.
        A class prompt that is the gate's own (`gate_class`) is no change:
        the gate it gives is the one there is."""
        text = " ".join((text or "").split())
        class_prompt = " ".join((class_prompt or "").split()) or None
        same_class = (class_prompt or self.gate_class) == (self.class_prompt or self.gate_class)
        if not text or (text == self.prompt and same_class):
            return False
        if self.retarget is None:
            logger.warning("the verifier's prompt is %r, but this reranker can't switch prompts", text)
            self.prompt = text  # warn once
            return False
        old = self.prompt
        self.selector, gate = self.retarget(text, class_prompt)
        if self.gate is not None or gate is not None:
            self.gate = gate
        self.prompt, self.class_prompt, self.has_prompt = text, class_prompt, True
        self.generation += 1
        dropped = self.queue.rescore(lambda tile: (False, tile.score, False))
        self._answered = []
        self.answers = Counter()
        if self.similar is not None:
            self.similar.forget()
        if self.repeats is not None:
            self.repeats.forget()
        self._relevance_thr, self._bar = None, self.min_prompt_score
        self._recent.clear()
        self._good_features.clear()
        logger.info("prompt: now %r (was %s), the verifier's%s; learning cleared, %d waiting crops dropped",
                    text, "none" if old is None else repr(old),
                    f", over its class {class_prompt!r}" if class_prompt and self.gate is not None else "",
                    len(dropped))
        return True

    @property
    def vetoing(self):
        """How many no's veto crops like them."""
        return sum(n >= self.selector.confirmations for n in self.selector.noes)

    def _still_unverified(self, tile):
        unveto = None
        if self.visual_guard and self.lift_vetoes:
            unveto = lambda i, fl: self._lifts(tile.features, fl)
        verdict = int(self.selector.score(tile.embedding, unveto)[1][0])
        if verdict == VETOED and tile.jumped:
            verdict = UNVERIFIED
        return verdict == UNVERIFIED, tile.score, tile.must_send

    def _lifts(self, feats, others):
        """Whether a confirmed background's veto is lifted for a tile with
        the features `feats`: only when the tile has enough keypoints for a
        match to be possible and matches none of the background's `others`.
        A featureless tile (black, blank, washed out) can't be told apart
        visually, so CLIP's veto stands."""
        return (feats is not None and self.visual.keypoints(feats) >= self.visual.min_matches
                and not self._like_any(feats, others))

    def _like_any(self, feats, others):
        """Whether the features `feats` match any of `others`' visually."""
        return feats is not None and any(self.visual.matches(feats, f) >= self.visual.min_matches for f in others)

    def _is_new(self, x, now):
        """Whether the crop with embedding `x` is unlike every crop queued
        and every one answered in the last cooldown_s."""
        self._answered = [(e, at) for e, at in self._answered if now - at < self.cooldown_s]
        seen = [tile.embedding for tile in self.queue] + [e for e, _ in self._answered]
        return all(float(x @ e) < self.novelty for e in seen)

    def _jumped(self, x, score):
        """Whether the crop with embedding `x` and detector score `score`
        jumped: some crop like it (within learn similarity) was asked about,
        and its score is at least score_jump times the best of theirs (and
        of score_jump_floor). A crop like none asked about is new anyway."""
        if not self.score_jump:
            return False
        like = [s for e, s in self._asked if float(x @ e) >= self.selector.similarity]
        return bool(like) and score >= self.score_jump * max(max(like), self.score_jump_floor)

    def _duplicates(self, kept, box):
        """Whether `box` duplicates the better-scoring candidate `kept`: the
        part of the smaller of the two inside the other is at least
        `dedupe_overlap`, and the larger is at most `dedupe_max_area_ratio`
        times the smaller's area."""
        iw = min(kept[2], box[2]) - max(kept[0], box[0])
        ih = min(kept[3], box[3]) - max(kept[1], box[1])
        if iw <= 0 or ih <= 0:
            return False
        a = (kept[2] - kept[0]) * (kept[3] - kept[1])
        b = (box[2] - box[0]) * (box[3] - box[1])
        small, large = min(a, b), max(a, b)
        return iw * ih >= self.dedupe_overlap * small and large <= self.dedupe_max_area_ratio * small

    # Frames

    def candidates(self, rgb, detections):
        """The candidates among `detections` in the frame `rgb` (of
        `classes`, at least `input_confidence`, not a duplicate of a better
        one: `dedupe_overlap`), best confidence first, embedded and scored.
        A duplicate doesn't take a place among the `max_candidates`."""
        height, width = rgb.shape[:2]
        found = []
        for detection in sorted(detections.detections, key=lambda d: d.score, reverse=True):
            if normalize_class(detection.class_name) not in self.classes:
                continue
            if detection.score < self.input_confidence:
                break  # sorted: every one after is lower
            box = pixel_box(detection.bbox, width, height)
            if box is None:
                continue
            if self.dedupe_overlap and any(self._duplicates(kept, box) for _, kept in found):
                continue
            found.append((detection, box))
            if len(found) == self.max_candidates:
                break
        if not found:
            return []
        size = self.encoder.size
        crops = []
        t0 = time.perf_counter()
        for _, box in found:
            x0, y0, x1, y1 = square_around(box, self.context, 0, width, height)
            crops.append(cv2.resize(rgb[y0:y1, x0:x1], (size, size), interpolation=cv2.INTER_LINEAR))
        t1 = time.perf_counter()
        v = self.encoder.encode_tiles(crops)
        t2 = time.perf_counter()
        self._time["crops"] += t1 - t0
        self._time["clip"] += t2 - t1
        self._time["embedded"] += len(crops)
        unveto = None
        if self.visual_guard and self.lift_vetoes:
            # A confirmed background vetoes a crop only if their keypoints match (or it has too few to tell)
            def unveto(i, fl):
                x0, y0, x1, y1 = square_around(found[i][1], self.context, 0, width, height)
                return self._lifts(self.visual.features(np.ascontiguousarray(rgb[y0:y1, x0:x1])), fl)
        s, verdict = self.selector.score(v, unveto)
        margin = self.gate.margins(v) if self.gate is not None else np.full(len(found), np.nan)
        out = [Candidate(d, box, v[i], float(s[i]), int(verdict[i]), float(margin[i]))
               for i, (d, box) in enumerate(found)]
        for c in out:
            c.jumped = self._jumped(c.embedding, c.score)
            if c.jumped and c.verdict == VETOED:
                c.verdict = UNVERIFIED
        self._time["score"] += time.perf_counter() - t2
        return out

    def _relevant(self, c):
        """Whether the candidate `c` passes the prompt filter: always without
        it (or without a prompt yet, when s means nothing), and always once
        confirmed; else its s must be above `min_prompt_score`."""
        return (not (self.prompt_filter and self.has_prompt) or c.verdict == CONFIRMED_TARGET
                or c.s > self._bar)

    def _update_bar(self, candidates):
        """The frame's prompt-filter bar: min_prompt_score, or with
        relevance_k, the higher of it and the adaptive bar after this frame's
        candidates (cliped's, over the candidates in place of tiles)."""
        if not (self.relevance_k and self.prompt_filter and self.has_prompt and candidates):
            self._bar = self.min_prompt_score
            return
        s = np.array([c.s for c in candidates])
        k = min(self.relevance_k, len(s))
        kth = float(np.partition(s, -k)[-k])
        self._relevance_thr = kth if self._relevance_thr is None else \
            self.relevance_ema * self._relevance_thr + (1 - self.relevance_ema) * kth
        self._bar = max(self.min_prompt_score, self._relevance_thr + self.relevance_margin * float(s.std()))

    def pick(self, candidates):
        """The index of the candidate to report, or None. A confirmed one
        wins; then, with rank "confidence", the most confident (plus
        prompt_weight times s), or with rank "prompt", the one most like the
        prompt: the frame's candidates are ranked against each other by s,
        as cliped ranks a frame's tiles, so its scale doesn't matter. With a
        gate, an unverified box must also be more like the prompt than the
        gate's learned cutoff, so a frame where the target isn't among the
        boxes reports none; with the prompt filter, its s must pass it."""
        eligible = [i for i, c in enumerate(candidates)
                    if c.verdict == CONFIRMED_TARGET
                    or (c.verdict == UNVERIFIED and not self.require_verified and c.score >= self.confidence
                        and (self.gate is None or self.gate.passes(c.margin)) and self._relevant(c))]
        if not eligible:
            return None
        if self.rank == RANK_PROMPT and self.has_prompt:
            return max(eligible, key=lambda i: (candidates[i].verdict, candidates[i].s, candidates[i].score))
        # Without a prompt (yet), s means nothing: by confidence alone
        weight = self.prompt_weight if self.has_prompt else 0.0
        return max(eligible, key=lambda i: (candidates[i].verdict, candidates[i].score + weight * candidates[i].s))

    def verify_payload(self, frame, rgb, box):
        """The serialized RawFrame the verifier is asked about: a square around
        `box` with verify_context, the box drawn in red, at most
        max_verify_side on a side."""
        height, width = rgb.shape[:2]
        square = square_around(box, self.verify_context, self.min_verify_side, width, height)
        x0, y0, x1, y1 = square
        crop = np.ascontiguousarray(rgb[y0:y1, x0:x1])
        left, top, right, bottom = box
        thickness = max(1, round((x1 - x0) / 100))
        cv2.rectangle(crop, (left - x0, top - y0), (right - x0 - 1, bottom - y0 - 1), BOX_COLOR, thickness)
        if x1 - x0 > self.max_verify_side:
            crop = cv2.resize(crop, (self.max_verify_side, self.max_verify_side), interpolation=cv2.INTER_AREA)
        height, width = crop.shape[:2]
        return tile_frame(frame, crop, (0, 0, width, height)), square

    def decide(self, data, outcome, now=None):
        """The serialized DetectionResult to report for the serialized RGB
        RawFrame `data`, given the detector's `outcome` for it (as `outcome`
        gave it), or b"" for none. Queues the crops to verify. Raises
        FrameError if `data` is not an RGB RawFrame."""
        if isinstance(outcome, str):
            self._counts[outcome] += 1
            return b""
        now = time.monotonic() if now is None else now
        started = time.perf_counter()
        frame, rgb = read_frame(data)
        self._time["read"] += time.perf_counter() - started
        height, width = rgb.shape[:2]
        candidates = self.candidates(rgb, outcome)
        self._update_bar(candidates)
        mark = time.perf_counter()
        chosen = self.pick(candidates)
        queued = set()
        for i, c in enumerate(candidates):
            if c.verdict != UNVERIFIED:
                continue
            if not self._relevant(c):
                self._counts["off the prompt"] += 1
                continue
            if not (c.jumped or self._is_new(c.embedding, now)):
                continue
            repeat = rfeats = None
            if self.repeats is not None:
                x0, y0, x1, y1 = square_around(c.box, self.context, 0, width, height)
                rfeats = self.repeats.features(np.ascontiguousarray(rgb[y0:y1, x0:x1]))
                repeat = self.repeats.like(c.embedding, rfeats, now)
                if repeat is not None and not (self.score_jump and c.score >= self.score_jump * max(
                        repeat[2], self.score_jump_floor)):
                    self.repeats.held_back(repeat, c.embedding, rfeats, now)
                    self._counts["repeat"] += 1
                    logger.debug("frame %d: box %d (score %.2f) repeats the crop of frame %d (score %.2f); held back",
                                 frame.id, i, c.score, repeat[4], repeat[2])
                    continue
                if repeat is not None:
                    self._counts["repeat jumped"] += 1
                    logger.info("frame %d: box %d repeats the crop of frame %d but its score %.2f jumped over %.2f; "
                                "sending", frame.id, i, repeat[4], c.score, repeat[2])
            payload, square = self.verify_payload(frame, rgb, c.box)
            tile = Tile(score=c.score, payload=payload, frame_id=frame.id, box=square, index=i,
                        embedding=c.embedding, must_send=i == chosen, generation=self.generation,
                        # Without a prompt (yet) s means nothing: by confidence
                        rank=c.s if self.queue_by == QUEUE_BY_PROMPT and self.has_prompt else None)
            if self.visual is not None or self.similar is not None:
                # The crop CLIP embedded, without the verifier's red box
                x0, y0, x1, y1 = square_around(c.box, self.context, 0, width, height)
                crop = np.ascontiguousarray(rgb[y0:y1, x0:x1])
                filt = self.visual or self.similar
                tile.features = filt.features(crop)
                if self.propagate_s:
                    self._recent.append((now, tile))
                if self.similar is not None:
                    tile.similar = not self.similar.is_new(crop, c.embedding, frame.id, feats=tile.features)
            if c.jumped:
                tile.jumped, tile.similar = True, False
                self._counts["jumped"] += 1
                logger.info("frame %d: box %d's score %.2f jumped over the crops like it asked about; asking",
                            frame.id, i, c.score)
            self.queue.push(tile)
            self._asked.append((c.embedding, c.score))
            if self.repeats is not None:
                self.repeats.sent(repeat, c.embedding, rfeats, c.score, now, frame.id)
            queued.add(i)
        done = time.perf_counter()
        self._time["queue"] += done - mark
        self._time["decide"] += done - started
        self._time["decided"] += 1
        ms = (done - started) * 1e3
        if self.candidate_log is not None:
            self.candidate_log.write(frame, width, height, candidates, chosen, queued, ms)
        self._count(candidates, chosen)
        if candidates:
            logger.debug("frame %d: %s -> %s in %.0f ms; queued %d", frame.id,
                         ", ".join(f"{c.detection.class_name} {c.score:.2f}/{'-0+'[c.verdict + 1]}" for c in candidates),
                         "none" if chosen is None else chosen, ms, len(queued))
        if chosen is None:
            return b""
        detection = result_pb2.Detection()
        detection.CopyFrom(candidates[chosen].detection)
        detection.class_name = self.class_name
        return result_pb2.DetectionResult(detections=[detection]).SerializeToString()

    def _count(self, candidates, chosen):
        self._counts["frames"] += 1
        self._counts["candidates"] += len(candidates)
        self._counts["vetoed"] += sum(c.verdict == VETOED for c in candidates)
        if chosen is None:
            self._counts["no detection" if not candidates else "all held back"] += 1
        else:
            self._counts["reported confirmed" if candidates[chosen].verdict == CONFIRMED_TARGET
                         else "reported unverified"] += 1
            self._counts["over the top box" if chosen != 0 else "top box"] += 1

    def _gate_summary(self):
        if self.gate is None:
            return ""
        cutoff = self.gate.cutoff
        return " | gate " + ("not cutting yet" if cutoff is None else f"cutoff {cutoff:+.3f}") + \
            f" (margins: yes {np.mean(self.gate.yes) if self.gate.yes else float('nan'):+.3f}, " \
            f"no {np.mean(self.gate.no) if self.gate.no else float('nan'):+.3f})"

    def summarize(self):
        """Logs what happened to the frames, if a summary is due."""
        now = self.clock()
        if self._summary_at_ns is None:
            self._summary_at_ns = now
        if now - self._summary_at_ns < self.summary_interval_ns:
            return
        if self._counts:
            c = self._counts
            logger.info(
                "last %.0f s: %d frames, %d candidates (%d vetoed%s) | reported %d confirmed, %d unverified "
                "(%d over the top box) | none: %d without candidates, %d all held back%s | verifier %d yes, %d no, %d unsure; "
                "queue %d (%d in flight)%s%s",
                (now - self._summary_at_ns) / 1e9, c["frames"], c["candidates"], c["vetoed"],
                f", {c['off the prompt']} off the prompt" if self.prompt_filter else "",
                c["reported confirmed"], c["reported unverified"], c["over the top box"], c["no detection"],
                c["all held back"],
                "".join(f", {c[why]} {why}" for why in (SKIPPED, TIMED_OUT, DISCONNECTED, UNDECODABLE, TOO_LATE)
                        if c[why]),
                self.answers["yes"], self.answers["no"], self.answers["unsure"], len(self.queue), self.queue.in_flight, self._gate_summary(),
                f" | repeats: {c['repeat']} held back, {c['repeat jumped']} sent on a jump"
                if self.repeats is not None else "")
        tm, n = self._time, self._time["decided"]
        if n:
            ms = {k: tm[k] / n * 1e3 for k in ("decide", "read", "crops", "clip", "score", "queue")}
            logger.info(
                "last %.0f s: %d frames decided in %.1f ms each: read %.1f, crops %.1f, CLIP %.1f (%.1f per crop, "
                "%.1f crops), score %.1f, pick and queue %.1f",
                (now - self._summary_at_ns) / 1e9, n, ms["decide"], ms["read"], ms["crops"], ms["clip"],
                tm["clip"] / tm["embedded"] * 1e3 if tm["embedded"] else 0.0, tm["embedded"] / n, ms["score"],
                ms["queue"])
        self._time = Counter()
        self._counts = Counter()
        self._summary_at_ns = now


def run(name, reranker, *, stop, on_ready=None):
    """Serves the engine `name`, deciding the frames it gets by the events of
    `reranker.detector`, and its reintegration producer, which delivers the
    crops to `reranker.verifier`, until the threading.Event `stop` is set."""
    producer = producer_name(name, REINTEGRATION)
    reintegration = Reintegration(reranker.queue, tile_log=reranker.tile_log)
    size = max_input_size(reranker.max_verify_side)
    with (
        uriel.Engine(name, result_type=DETECTION_RESULT_TYPE, max_output_size=MAX_RESULT_SIZE) as engine,
        uriel.Producer(producer, input_type=RAW_FRAME_TYPE, max_input_size=size) as crops,
        uriel.Subscription.producer(producer) as events,
    ):
        engine.subscribe(reranker.detector)
        # For the verifier's prompt heartbeat
        if not reranker.ignore_new:
            engine.subscribe(reranker.verifier)

        def publish(payload):
            return uriel.InputId(producer, crops.session, crops.send(payload))

        logger.info("engine %s ready, with producer %s; reranking %s's %s as %r, verified by %s",
                    name, producer, reranker.detector, sorted(reranker.classes), reranker.class_name,
                    reranker.verifier)
        if on_ready is not None:
            on_ready()
        held, deadline = None, None
        while not stop.is_set():
            timeout = POLL_INTERVAL_S
            if deadline is not None:
                timeout = min(timeout, max((deadline - time.monotonic_ns()) / 1e9, 0.0))
            if timeout > 0:
                engine.wait(timeout)
            while (event := engine.poll_event()) is not None:
                try:
                    if not reranker.on_prompt_event(event):
                        reranker.on_detector_event(event)
                except Exception:
                    logger.exception("failed to handle a %s event", event.kind)
            while (event := events.poll()) is not None:
                try:
                    reranker.on_queue_event(event)
                except Exception:
                    logger.exception("failed to handle a %s event", event.kind)
            while True:
                if held is None:
                    held = engine.poll()
                    if held is None:
                        deadline = None
                        break
                    if held.payload_type and type_name(held.payload_type) != RAW_FRAME_TYPE:
                        logger.warning("input %d is a %r, not a RawFrame; dropping it", held.id.seq,
                                       held.payload_type)
                        engine.drop()
                        held = None
                        continue
                    deadline = reranker.deadline()
                outcome = reranker.outcome(held.id, deadline)
                if outcome is None:
                    break
                _finish(engine, reranker, held, outcome)
                held, deadline = None, None
            try:
                reintegration.step(time.monotonic(), publish)
            except Exception:
                logger.exception("reintegration failed to send a crop")
            reranker.summarize()


def _finish(engine, reranker, input, outcome):
    """Drops the held `input`, with the detection reported, if any."""
    try:
        result = reranker.decide(bytes(input.data), outcome)
    except FrameError as err:
        logger.warning("input %d: %s; dropping it", input.id.seq, err)
        result = b""
    except Exception:
        logger.exception("input %d: failed to decide it; dropping it", input.id.seq)
        result = b""
    if result:
        engine.drop(result=result)
    else:
        engine.drop()
