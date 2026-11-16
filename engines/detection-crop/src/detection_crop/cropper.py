"""Matches each frame the engine holds with the detector's result for the same
input, decides what to forward in its place, and queues each crop for the
engine's reintegration producer, which delivers it to the verifier
(send_queue.py).

The engine subscribes to the detector's events. For each input, the detector
either:

- has a result: a RESULT event. A detector that drops the input with no
  result found nothing in it (detection-qnn does this with early_discard);
- failed: a SKIPPED or TIMED_OUT event, or a DISCONNECTED event, which fails
  every frame until the detector is heard from again.

The result may come before or after the frame. A frame is cropped to the
highest-scoring detection of the class in the result, and dropped if there is
none, if the detector failed, or if its timeout, counted from when the frame
reaches the engine, ends first.

The crop forwarded in place of a frame is not tracked: in a flow where the
engine is a dead end, it goes nowhere. The events about the reintegration
producer's inputs settle the crops in the queue (engine.run).
"""

import logging
from collections import Counter, OrderedDict

import uriel

from detection_crop.crop import (
    FrameError,
    best_detection,
    classification,
    crop,
    parse_frame,
    parse_result,
    pixel_box,
)
from detection_crop.send_queue import Crop

logger = logging.getLogger("detection_crop")

# The most detector outcomes kept for frames that have not arrived. Outcomes of
# inputs the engine never gets are forgotten after this many.
MAX_OUTCOMES = 1024
# How often what happened to the frames is summarized
SUMMARY_INTERVAL_NS = 30 * 1_000_000_000

# Why a frame was dropped
SKIPPED = "detector skipped"
TIMED_OUT = "detector timed out"
DISCONNECTED = "detector disconnected"
UNDECODABLE = "undecodable result"
TOO_LATE = "result too late"
UNREADABLE = "unreadable frame"
NO_DETECTION = "no detection of the class"
OUTSIDE = "box outside the frame"
TOO_BIG = "crop too big"
CROPPED = "cropped"

_FAILED = {
    uriel.EventKind.SKIPPED: SKIPPED,
    uriel.EventKind.TIMED_OUT: TIMED_OUT,
}


class Cropper:
    def __init__(
        self,
        *,
        detector,
        class_name,
        verifier,
        queue,
        timeout_ns,
        max_output_size,
        clock,
        summary_interval_ns=SUMMARY_INTERVAL_NS,
        sent_log=None,
    ):
        self.detector = detector
        self.class_name = class_name
        self.verifier = verifier
        # A send_queue.SendQueue, which holds the crops until the verifier
        # has them; Reintegration sends the ones waiting
        self.queue = queue
        self.timeout_ns = timeout_ns
        self.max_output_size = max_output_size
        self.clock = clock
        self.summary_interval_ns = summary_interval_ns
        # A sent_log.SentLog each crop queued is logged to, or None
        self.sent_log = sent_log
        # Per input id: the detector's DetectionResult, or why it has none
        self._outcomes = OrderedDict()
        self._disconnected = False
        self._counts = Counter()
        self._summary_at_ns = None

    def on_event(self, event):
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
            # The frames whose results were lost time out
            logger.warning("%d events of %s were lost", event.lost, self.detector)
            return
        if kind not in (uriel.EventKind.RESULT, *_FAILED):
            return
        if self._disconnected:
            logger.info("%s is reachable again", self.detector)
            self._disconnected = False
        if kind == uriel.EventKind.RESULT:
            outcome = parse_result(event.payload_type, event.result)
            if outcome is None:
                logger.warning(
                    "input %d: %s sent a %r of %d bytes, not a DetectionResult",
                    event.id.seq,
                    self.detector,
                    event.payload_type,
                    len(event.result),
                )
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
        """The detector's DetectionResult for the input `id`, or why the frame
        is dropped without one; None while it is still worth waiting for, up
        to `deadline`."""
        outcome = self._outcomes.pop(id, None)
        if outcome is not None:
            return outcome
        if self._disconnected:
            return DISCONNECTED
        if self.clock() >= deadline:
            return TOO_LATE
        return None

    def apply(self, id, data, outcome, now=None):
        """The crop to send in place of the input `id` (a send_queue.Crop),
        its serialized RawFrame `data` cropped by `outcome` (as the method
        `outcome` gave it), and its result, the serialized
        ClassificationResult of the detection; or (None, b"") if the frame is
        dropped. The crop is queued for reintegration; the one forwarded is
        not tracked."""
        if isinstance(outcome, str):
            return self._drop(id, outcome)
        detection = best_detection(outcome, self.class_name)
        if detection is None:
            return self._drop(id, NO_DETECTION)
        try:
            frame, pixels = parse_frame(data)
        except FrameError as err:
            logger.warning("input %d: %s", id.seq, err)
            return self._drop(id, UNREADABLE)
        width, height = frame.h_res, frame.v_res
        out = crop(frame, pixels, detection.bbox)
        if out is None:
            return self._drop(id, OUTSIDE)
        result = classification(detection)
        if len(out) + len(result) > self.max_output_size:
            logger.warning(
                "input %d: a %dx%d crop and its result are %d bytes, over %d",
                id.seq,
                frame.h_res,
                frame.v_res,
                len(out) + len(result),
                self.max_output_size,
            )
            return self._drop(id, TOO_BIG)
        self._count(CROPPED)
        if self.sent_log is not None:
            box = pixel_box(detection.bbox, width, height)
            self.sent_log.write(frame, box, width, height, detection.score, detection.class_name)
        sent = Crop(score=detection.score, payload=out, frame_id=frame.id)
        self.queue.push(sent)
        return sent, result

    def _drop(self, id, why):
        logger.debug("input %d: dropped (%s)", id.seq, why)
        self._count(why)
        return None, b""

    def _count(self, what):
        self._counts[what] += 1

    def summarize(self):
        """Logs what happened to the frames, if a summary is due."""
        now = self.clock()
        if self._summary_at_ns is None:
            self._summary_at_ns = now
        if now - self._summary_at_ns < self.summary_interval_ns:
            return
        if self._counts:
            seconds = (now - self._summary_at_ns) / 1e9
            cropped = self._counts.pop(CROPPED, 0)
            dropped = ", ".join(f"{n} {why}" for why, n in self._counts.most_common())
            logger.info(
                "last %.0f s: %d frames cropped%s; queue %d (%d in flight), %d confirmed",
                seconds,
                cropped,
                f" | dropped: {dropped}" if dropped else "",
                len(self.queue),
                self.queue.in_flight,
                self.queue.confirmed,
            )
        self._counts = Counter()
        self._summary_at_ns = now
