"""Matches each frame the engine receives with the detector's result for the
same input, and saves the two together.

The engine subscribes to the detector's events. For each input, the detector
either:

- has a result: a RESULT event. A detector that drops the input with no
  result found nothing in it (detection-qnn does this with early_discard), so
  the frame is saved with no boxes;
- failed: a SKIPPED or TIMED_OUT event, or a DISCONNECTED event, which fails
  every frame waiting on it. The frame is not saved, since nothing was
  detected in it.

The result may come before or after the frame. A frame still waiting when its
timeout ends, counted from when it reaches the engine, is not saved.
"""

import logging
from collections import Counter, OrderedDict

import uriel
from google.protobuf.message import DecodeError
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.result import result_pb2

logger = logging.getLogger("detection_log")

RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"
DETECTION_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/DetectionResult"
# The most detector outcomes kept for frames that have not arrived. Outcomes of
# inputs the engine never gets are forgotten after this many.
MAX_OUTCOMES = 1024
# The most frames waiting on the detector; past this, the oldest is given up.
# A 1280x720 RGB frame is 2.7 MB.
MAX_WAITING = 32
# How often what happened to the frames is summarized
SUMMARY_INTERVAL_NS = 30 * 1_000_000_000

# Why a frame was not saved
SKIPPED = "detector skipped"
TIMED_OUT = "detector timed out"
DISCONNECTED = "detector disconnected"
UNDECODABLE = "undecodable result"
TOO_LATE = "result too late"
TOO_MANY = "too many waiting"
UNREADABLE = "unreadable frame"
SAVE_BEHIND = "saving behind"

_FAILED = {
    uriel.EventKind.SKIPPED: SKIPPED,
    uriel.EventKind.TIMED_OUT: TIMED_OUT,
}


def _type_name(payload_type):
    # Engines write a type's name with slashes or, as detection-qnn does, dots
    return payload_type.replace(".", "/")


def parse_result(event):
    """The DetectionResult of the detector's RESULT `event`, or None if it is
    not one."""
    if event.payload_type and _type_name(event.payload_type) != DETECTION_RESULT_TYPE:
        return None
    try:
        return result_pb2.DetectionResult.FromString(event.result)
    except DecodeError:
        return None


def parse_frame(input):
    """The RawFrame of `input`, or None if it is not one."""
    if input.payload_type and _type_name(input.payload_type) != RAW_FRAME_TYPE:
        return None
    try:
        return frame_pb2.RawFrame.FromString(bytes(input.data))
    except DecodeError:
        return None


class DetectionLog:
    def __init__(
        self,
        *,
        detector,
        saver,
        timeout_ns,
        clock,
        summary_interval_ns=SUMMARY_INTERVAL_NS,
    ):
        self.detector = detector
        self.saver = saver
        self.timeout_ns = timeout_ns
        self.clock = clock
        self.summary_interval_ns = summary_interval_ns
        # Per input id: the detector's DetectionResult, or why it has none
        self._outcomes = OrderedDict()
        # Per input id: (RawFrame, when to give up on it)
        self._waiting = OrderedDict()
        self._disconnected = False
        self._counts = Counter()
        self._summary_at_ns = None

    # Events

    def on_event(self, event):
        if event.engine != self.detector:
            return
        kind = event.kind
        if kind == uriel.EventKind.DISCONNECTED:
            logger.info("%s disconnected", self.detector)
            self._disconnected = True
            for id in list(self._waiting):
                self._give_up(id, DISCONNECTED)
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
            outcome = parse_result(event)
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
        if event.id in self._waiting:
            self._finish(event.id, outcome)
            return
        self._outcomes[event.id] = outcome
        while len(self._outcomes) > MAX_OUTCOMES:
            self._outcomes.popitem(last=False)

    # Frames

    def on_frame(self, id, frame):
        """Takes the RawFrame `frame` of the input `id` (None if it could not
        be read), and saves it once the detector's result for it is in."""
        if frame is None:
            self._outcomes.pop(id, None)
            self._count(UNREADABLE)
            return
        outcome = self._outcomes.pop(id, None)
        if outcome is not None:
            self._waiting[id] = (frame, None)
            self._finish(id, outcome)
            return
        if self._disconnected:
            self._count(DISCONNECTED)
            return
        self._waiting[id] = (frame, self.clock() + self.timeout_ns)
        while len(self._waiting) > MAX_WAITING:
            self._give_up(next(iter(self._waiting)), TOO_MANY)

    def expire(self):
        """Gives up on the frames whose timeout has ended, and logs the
        summary if it is due. Returns when the next frame times out, or None
        if no frame is waiting."""
        now = self.clock()
        for id, (_, deadline) in list(self._waiting.items()):
            if deadline <= now:
                self._give_up(id, TOO_LATE)
        self._summarize(now)
        return min((deadline for _, deadline in self._waiting.values()), default=None)

    def _finish(self, id, outcome):
        frame, _ = self._waiting.pop(id)
        if isinstance(outcome, str):
            self._count(outcome)
        elif self.saver.submit(id.seq, frame, outcome):
            self._count("saved")
        else:
            self._count(SAVE_BEHIND)

    def _give_up(self, id, why):
        self._waiting.pop(id)
        logger.debug("input %d: not saved (%s)", id.seq, why)
        self._count(why)

    def _count(self, what):
        self._counts[what] += 1

    def _summarize(self, now):
        if self._summary_at_ns is None:
            self._summary_at_ns = now
        if now - self._summary_at_ns < self.summary_interval_ns:
            return
        if self._counts:
            seconds = (now - self._summary_at_ns) / 1e9
            saved = self._counts.pop("saved", 0)
            not_saved = ", ".join(f"{n} {why}" for why, n in self._counts.most_common())
            logger.info(
                "last %.0f s: %d frames saved%s",
                seconds,
                saved,
                f" | not saved: {not_saved}" if not_saved else "",
            )
        self._counts = Counter()
        self._summary_at_ns = now

    @property
    def waiting(self):
        """How many frames are waiting on the detector."""
        return len(self._waiting)
