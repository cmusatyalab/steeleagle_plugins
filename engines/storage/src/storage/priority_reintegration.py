"""Priority reintegration: while the remote engine is unreachable, store the
frames, and once it is back, send it the stored frames the local classifier
found the target class in, highest score first.

The engine moves between three modes:

- IDLE, at the start: it drops every frame without storing it.
- STORING, from a DISCONNECTED event of the remote engine: it stores every
  frame and drops it. Each RESULT event of the classifier whose
  ClassificationResult is of the target class queues that frame, with the
  result's score as its priority.
- REINTEGRATING, from a SENT, ACCEPTED or RESULT event of the remote engine
  while STORING: it no longer stores frames, and sends a queued frame in place
  of each input, one at a time. After sending one, it drops inputs until the
  remote engine's SENT event for the input the frame went out as; a SKIPPED,
  TIMED_OUT or LOST event instead sends the same frame again in place of the
  next input. A DISCONNECTED event returns it to STORING, with the unconfirmed
  frame queued again.
"""

import enum
import heapq
import itertools
import logging
from dataclasses import dataclass

import uriel
from google.protobuf.message import DecodeError

from steeleagle_protocol.v1.messages.result import result_pb2
from storage.store import FrameError, FrameKey, frame_index, read_frame

logger = logging.getLogger("storage.priority_reintegration")

CLASSIFICATION_RESULT = "ClassificationResult"
# The remote engine's events that show it is reachable again.
_RECONNECTED = frozenset(
    {uriel.EventKind.SENT, uriel.EventKind.ACCEPTED, uriel.EventKind.RESULT}
)
# The remote engine's events that end an offer without it being sent.
_NOT_SENT = frozenset({uriel.EventKind.SKIPPED, uriel.EventKind.TIMED_OUT})


class Mode(enum.Enum):
    IDLE = "idle"
    STORING = "storing"
    REINTEGRATING = "reintegrating"


@dataclass
class InFlight:
    """The stored frame being reintegrated."""

    key: FrameKey
    score: float
    # The id of the input the frame went out in place of, whose SENT event
    # confirms it; None if it must be sent (again) in place of the next input.
    carrier: uriel.InputId | None = None


def result_type_name(payload_type):
    """The message name of a payload type, e.g. "ClassificationResult" for
    "steeleagle_protocol/v1/messages/result/ClassificationResult"."""
    return payload_type.replace(".", "/").rsplit("/", 1)[-1]


class PriorityReintegration:
    def __init__(self, store, *, remote, classifier, target_class, max_frame_size):
        self.store = store
        self.remote = remote
        self.classifier = classifier
        self.target_class = target_class.strip().casefold()
        self.max_frame_size = max_frame_size
        self.mode = Mode.IDLE
        self.in_flight = None
        # Max-heap of (-score, order, key); `queued` has each key's best
        # score, and entries with another score are stale.
        self._heap = []
        self._queued = {}
        self._order = itertools.count()

    # Events

    def on_event(self, event):
        if event.engine == self.remote:
            self._remote_event(event)
        elif event.engine == self.classifier:
            self._classifier_event(event)

    def _remote_event(self, event):
        kind = event.kind
        if kind == uriel.EventKind.DISCONNECTED:
            if self.mode != Mode.STORING:
                logger.info("%s disconnected; storing frames", self.remote)
            if self.in_flight is not None:
                in_flight, self.in_flight = self.in_flight, None
                self._enqueue(in_flight.key, in_flight.score)
            self.mode = Mode.STORING
        elif self.mode == Mode.STORING and kind in _RECONNECTED:
            logger.info(
                "%s is reachable again; reintegrating %d frames",
                self.remote,
                len(self._queued),
            )
            self.mode = Mode.REINTEGRATING
        elif self.mode == Mode.REINTEGRATING and self._awaiting():
            if kind == uriel.EventKind.LOST:
                # The SENT event may have been among those lost
                self._resend("events were lost")
            elif event.id == self.in_flight.carrier:
                if kind == uriel.EventKind.SENT:
                    logger.info("frame %d reintegrated", self.in_flight.key.seq)
                    self.in_flight = None
                elif kind in _NOT_SENT:
                    self._resend(kind.name)

    def _classifier_event(self, event):
        if (
            event.kind != uriel.EventKind.RESULT
            or result_type_name(event.payload_type) != CLASSIFICATION_RESULT
        ):
            return
        try:
            result = result_pb2.ClassificationResult.FromString(event.result)
        except DecodeError as err:
            logger.warning("input %d: bad ClassificationResult: %s", event.id.seq, err)
            return
        if not result.HasField("class"):
            return
        if getattr(result, "class").strip().casefold() != self.target_class:
            return
        key = FrameKey.of(event.id)
        # A frame is queued only if it is or will be stored; the classifier
        # may finish a frame after the engine stored it.
        if self.mode == Mode.STORING or key in self.store:
            self._enqueue(key, result.score)

    # Inputs

    def on_input(self, input):
        try:
            frame = read_frame(input.payload_type, input.data)
        except FrameError as err:
            logger.warning("input %d: %s", input.id.seq, err)
            return None
        if self.mode == Mode.STORING:
            if len(input.data) > self.max_frame_size:
                logger.warning(
                    "input %d is %d bytes, over max_output_size; not storing it",
                    input.id.seq,
                    len(input.data),
                )
            else:
                self.store.add(
                    FrameKey.of(input.id),
                    input.payload_type,
                    input.data,
                    frame_index(frame),
                )
            return None
        if self.mode == Mode.REINTEGRATING:
            return self._next_frame(input.id)
        return None

    def _next_frame(self, carrier):
        """The stored frame to send in place of the input `carrier`, if any."""
        if self._awaiting():
            return None
        if self.in_flight is None:
            self.in_flight = self._dequeue()
            if self.in_flight is None:
                return None
        stored = self.store.get(self.in_flight.key)
        if stored is None:
            logger.warning("frame %d is no longer stored", self.in_flight.key.seq)
            self.in_flight = None
            return None
        self.in_flight.carrier = carrier
        logger.info(
            "reintegrating frame %d (score %.3f) as input %d",
            stored.key.seq,
            self.in_flight.score,
            carrier.seq,
        )
        return stored

    # The queue

    def _awaiting(self):
        return self.in_flight is not None and self.in_flight.carrier is not None

    def _resend(self, why):
        logger.info("frame %d was not sent (%s); retrying", self.in_flight.key.seq, why)
        self.in_flight.carrier = None

    def _enqueue(self, key, score):
        if self.in_flight is not None and self.in_flight.key == key:
            self.in_flight.score = max(self.in_flight.score, score)
            return
        if key in self._queued and self._queued[key] >= score:
            return
        self._queued[key] = score
        heapq.heappush(self._heap, (-score, next(self._order), key))

    def _dequeue(self):
        """The queued frame with the highest score, which is stored."""
        while self._heap:
            neg_score, _, key = heapq.heappop(self._heap)
            if self._queued.get(key) != -neg_score:
                continue
            del self._queued[key]
            if key in self.store:
                return InFlight(key, -neg_score)
        return None

    @property
    def queued(self):
        """How many frames wait to be reintegrated."""
        return len(self._queued)
