"""Matches each frame the engine holds with the detector's result for the same
input, and decides what to send in its place.

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
"""

import logging
from collections import Counter, OrderedDict

import uriel

from detection_crop.crop import FrameError, best_detection, crop, parse_frame, parse_result

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
        timeout_ns,
        max_output_size,
        clock,
        summary_interval_ns=SUMMARY_INTERVAL_NS,
    ):
        self.detector = detector
        self.class_name = class_name
        self.timeout_ns = timeout_ns
        self.max_output_size = max_output_size
        self.clock = clock
        self.summary_interval_ns = summary_interval_ns
        # Per input id: the detector's DetectionResult, or why it has none
        self._outcomes = OrderedDict()
        self._disconnected = False
        self._counts = Counter()
        self._summary_at_ns = None

    def on_event(self, event):
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

    def apply(self, id, data, outcome):
        """The serialized RawFrame `data` of the input `id`, cropped by
        `outcome` (as the method `outcome` gave it) and serialized; or None if
        the frame is dropped."""
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
        out = crop(frame, pixels, detection.bbox)
        if out is None:
            return self._drop(id, OUTSIDE)
        if len(out) > self.max_output_size:
            logger.warning(
                "input %d: a %dx%d crop is %d bytes, over %d",
                id.seq,
                frame.h_res,
                frame.v_res,
                len(out),
                self.max_output_size,
            )
            return self._drop(id, TOO_BIG)
        self._count(CROPPED)
        return out

    def _drop(self, id, why):
        logger.debug("input %d: dropped (%s)", id.seq, why)
        self._count(why)
        return None

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
                "last %.0f s: %d frames cropped%s",
                seconds,
                cropped,
                f" | dropped: {dropped}" if dropped else "",
            )
        self._counts = Counter()
        self._summary_at_ns = now
