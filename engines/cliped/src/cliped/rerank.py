"""box_rerank: picks which of a local detector's boxes, if any, is the target,
by what a remote verifier said about boxes that looked like it before.

The engine gets the same camera frames as a detection engine on this daemon
(e.g. detection-qnn running YOLO) and subscribes to its events. For each
frame it waits for the detector's DetectionResult, crops the candidates (the
detections of `classes`, best `max_candidates` by confidence) with a little
context around each, and embeds each crop with the CLIP image encoder. What
the verifier said about earlier crops (cliped.clip.Selector's learning, the
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

Learning runs off the frame path. Each unverified candidate is queued for the
verifier, unless it is like a crop already queued, or one the verifier
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
from collections import Counter, OrderedDict
from dataclasses import dataclass

import cv2
import numpy as np
import uriel
from google.protobuf.message import DecodeError

from cliped.clip import CONFIRMED, CONFIRMED_GOOD, DROPPED_GOOD
from cliped.engine import (
    OUTPUT_HEADROOM,
    POLL_INTERVAL_S,
    RAW_FRAME_TYPE,
    REINTEGRATION,
    RGB_CHANNELS,
    FrameError,
    Reintegration,
    producer_name,
    read_frame,
    read_verdict,
    tile_frame,
)
from cliped.send_queue import Tile
from cliped.sent_log import log_path
from steeleagle_protocol.v1.messages.result import result_pb2

logger = logging.getLogger("cliped.rerank")

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


def type_name(payload_type):
    """Engines write a type's name with slashes or, as detection-qnn does,
    dots."""
    return payload_type.replace(".", "/")


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

    @property
    def score(self):
        return self.detection.score


class CandidateLog:
    """One CSV row per candidate of every frame decided, so that what was
    reported can be scored against labeled boxes: the frame's id and capture
    timestamp, the candidate's number in the frame (best confidence first),
    its class, confidence, box in pixels, prompt score s, verdict (-1 vetoed,
    0 unverified, 1 confirmed), whether it was reported and whether it was
    queued for the verifier, and the milliseconds the frame took.
    Line-buffered."""

    HEADER = ("frame_id", "timestamp_ns", "candidate", "class", "score", "left", "top", "right", "bottom",
              "frame_width", "frame_height", "s", "verdict", "reported", "queued", "ms")

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
                                   f"{ms:.1f}"))

    def close(self):
        self._file.close()


class Reranker:
    """Decides each frame from the detector's result and what the verifier
    said. `encoder` embeds crops, `selector` (clip.Selector, with learning)
    holds what the verifier said, and `queue` (send_queue.SendQueue) the
    crops waiting for it; see the module for the rest."""

    def __init__(self, encoder, selector, queue, *, detector, verifier, class_name, classes=(), confidence=0.5,
                 max_candidates=8, context=1.25, verify_context=2.0, min_verify_side=96, max_verify_side=256,
                 novelty=0.92, cooldown_s=5.0, require_verified=False, prompt_weight=0.0, timeout_ns=1_000_000_000,
                 clock=time.monotonic_ns, tile_log=None, candidate_log=None,
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
        self.context = context
        self.verify_context = verify_context
        self.min_verify_side = min_verify_side
        self.max_verify_side = max_verify_side
        self.novelty = novelty
        self.cooldown_s = cooldown_s
        self.require_verified = require_verified
        self.prompt_weight = prompt_weight
        self.timeout_ns = timeout_ns
        self.clock = clock
        self.tile_log = tile_log
        self.candidate_log = candidate_log
        self.summary_interval_ns = summary_interval_ns
        # Per input id: the detector's DetectionResult, or why it has none
        self._outcomes = OrderedDict()
        self._disconnected = False
        # (embedding, time.monotonic()) of each crop the verifier answered
        # within the cooldown
        self._answered = []
        self._counts = Counter()
        self._summary_at_ns = None
        self.answers = Counter()

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
        tile = self.queue.on_event(event, self.verifier)
        if tile is None or tile.embedding is None:
            return
        match = read_verdict(event)
        if match is None:
            return
        now = time.monotonic() if now is None else now
        self._answered.append((tile.embedding, now))
        self.answers["yes" if match else "no"] += 1
        outcome = self.selector.learn(tile.embedding, match)
        message = "verifier: %s for the box of frame %d (score %.2f): %s (%d good, %d confirmed; %d no's, %d vetoing)"
        args = ["yes" if match else "no", tile.frame_id, tile.score, outcome, len(self.selector.good),
                len(self.selector.confirmed_good), len(self.selector.backgrounds), self.vetoing]
        if outcome in (CONFIRMED_GOOD, CONFIRMED, DROPPED_GOOD):
            dropped = self.queue.rescore(self._still_unverified)
            message += "; dropped %d queued crops it settled (%d left)"
            args += [len(dropped), len(self.queue)]
        logger.info(message, *args)

    @property
    def vetoing(self):
        """How many no's veto crops like them."""
        return sum(n >= self.selector.confirmations for n in self.selector.noes)

    def _still_unverified(self, tile):
        verdict = int(self.selector.score(tile.embedding)[1][0])
        return verdict == UNVERIFIED, tile.score, tile.must_send

    def _is_new(self, x, now):
        """Whether the crop with embedding `x` is unlike every crop queued
        and every one answered in the last cooldown_s."""
        self._answered = [(e, at) for e, at in self._answered if now - at < self.cooldown_s]
        seen = [tile.embedding for tile in self.queue] + [e for e, _ in self._answered]
        return all(float(x @ e) < self.novelty for e in seen)

    # Frames

    def candidates(self, rgb, detections):
        """The candidates among `detections` in the frame `rgb`, best
        confidence first, embedded and scored."""
        height, width = rgb.shape[:2]
        found = []
        for detection in sorted(detections.detections, key=lambda d: d.score, reverse=True):
            if normalize_class(detection.class_name) not in self.classes:
                continue
            box = pixel_box(detection.bbox, width, height)
            if box is not None:
                found.append((detection, box))
            if len(found) == self.max_candidates:
                break
        if not found:
            return []
        size = self.encoder.size
        crops = []
        for _, box in found:
            x0, y0, x1, y1 = square_around(box, self.context, 0, width, height)
            crops.append(cv2.resize(rgb[y0:y1, x0:x1], (size, size), interpolation=cv2.INTER_LINEAR))
        v = self.encoder.encode_tiles(crops)
        s, verdict = self.selector.score(v)
        return [Candidate(d, box, v[i], float(s[i]), int(verdict[i])) for i, (d, box) in enumerate(found)]

    def pick(self, candidates):
        """The index of the candidate to report, or None."""
        eligible = [i for i, c in enumerate(candidates)
                    if c.verdict == CONFIRMED_TARGET
                    or (c.verdict == UNVERIFIED and not self.require_verified and c.score >= self.confidence)]
        if not eligible:
            return None
        return max(eligible, key=lambda i: (candidates[i].verdict,
                                            candidates[i].score + self.prompt_weight * candidates[i].s))

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
        height, width = rgb.shape[:2]
        candidates = self.candidates(rgb, outcome)
        chosen = self.pick(candidates)
        queued = set()
        for i, c in enumerate(candidates):
            if c.verdict != UNVERIFIED or not self._is_new(c.embedding, now):
                continue
            payload, square = self.verify_payload(frame, rgb, c.box)
            tile = Tile(score=c.score, payload=payload, frame_id=frame.id, box=square, index=i,
                        embedding=c.embedding, must_send=i == chosen)
            self.queue.push(tile)
            queued.add(i)
        ms = (time.perf_counter() - started) * 1e3
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
                "last %.0f s: %d frames, %d candidates (%d vetoed) | reported %d confirmed, %d unverified "
                "(%d over the top box) | none: %d without candidates, %d all held back%s | verifier %d yes, %d no; "
                "queue %d (%d in flight)",
                (now - self._summary_at_ns) / 1e9, c["frames"], c["candidates"], c["vetoed"],
                c["reported confirmed"], c["reported unverified"], c["over the top box"], c["no detection"],
                c["all held back"],
                "".join(f", {c[why]} {why}" for why in (SKIPPED, TIMED_OUT, DISCONNECTED, UNDECODABLE, TOO_LATE)
                        if c[why]),
                self.answers["yes"], self.answers["no"], len(self.queue), self.queue.in_flight)
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
