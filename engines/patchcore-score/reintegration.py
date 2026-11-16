"""Prioritized reintegration of anomalous frames, as detection-crop does for
its crops (vendored from detection-crop's send_queue.py and engine.py, with
crops as frames).

The engine forwards each anomalous frame blindly and does not track it: in a
flow where the engine is a dead end, it goes nowhere. It also queues each,
scored by how far its anomaly score is above the threshold, and the engine's
reintegration producer delivers the queue: one frame at a time, most
anomalous first, each until the verifier's RESULT event for it shows the
verifier read it. (Any engine that finishes the frame posts a RESULT, even
with no result, so the verifier can be the decoder at the far end.) Give the
producer one token: the flow then paces it.

Every input ends in the flow with an event, so no timer is needed:

- the verifier's RESULT: it leaves the queue;
- SKIPPED or TIMED_OUT at any engine, or a RESULT of another engine that
  dropped it (before the verifier): its turn ended unread, so it waits to be
  sent again;
- DISCONNECTED, or LOST (the event that settled it may be among the lost):
  the frame in flight waits to be sent again.

ACCEPTED and SENT leave it in flight. A frame put back may reach the verifier
twice; that is the price of never losing one. Past `max_size`, the frame with
the lowest score is dropped.
"""

import itertools
import logging
from dataclasses import dataclass, field

import uriel

logger = logging.getLogger("patchcore_score.reintegration")

# Events that end a crop's offer at an engine without it being processed.
_NOT_ACCEPTED = frozenset({uriel.EventKind.SKIPPED, uriel.EventKind.TIMED_OUT})
# Events after which no crop in flight can be counted on.
_RESET = frozenset({uriel.EventKind.DISCONNECTED, uriel.EventKind.LOST})


@dataclass(eq=False)
class Crop:
    """An anomalous frame waiting for the verifier (named as detection-crop's
    crops, from which this is vendored)."""

    # How far above the threshold its anomaly score is (score / threshold)
    score: float
    # The serialized RawFrame of the crop, as it is sent
    payload: bytes
    # The sequence number of the camera input it was scored from, for logs
    frame_id: int
    order: int = 0
    # The ids of every input it went out as; the last is the latest
    carriers: list = field(default_factory=list)
    # When it was last sent (time.monotonic()), or None while it waits
    sent_at: float | None = None

    @property
    def in_flight(self):
        return self.sent_at is not None

    @property
    def priority(self):
        """Higher goes first: score, then the oldest."""
        return (self.score, -self.order)


class SendQueue:
    def __init__(self, *, max_size):
        if max_size < 1:
            raise ValueError("max_size must be at least 1")
        self.max_size = max_size
        self._crops = []
        # Carrier id -> the crop it carried
        self._carried = {}
        self._order = itertools.count()
        # How many crops the verifier confirmed
        self.confirmed = 0

    def __len__(self):
        return len(self._crops)

    def __contains__(self, crop):
        return crop in self._crops

    @property
    def in_flight(self):
        return sum(crop.in_flight for crop in self._crops)

    def push(self, crop):
        """Queues `crop`, dropping the lowest-priority crop if the queue is
        over its size. Returns the crop dropped, if any."""
        crop.order = next(self._order)
        self._crops.append(crop)
        if len(self._crops) <= self.max_size:
            return None
        # Lowest priority; of equal ones, the newest
        worst = min(self._crops, key=lambda c: c.priority)
        self._remove(worst)
        logger.debug(
            "queue full; dropped the crop of frame %d (score %.3f)", worst.frame_id, worst.score
        )
        return worst

    def best(self):
        """The highest-priority crop not in flight, or None if every crop is
        in flight or there are none."""
        waiting = [crop for crop in self._crops if not crop.in_flight]
        return max(waiting, key=lambda c: c.priority, default=None)

    def sent(self, crop, carrier, now):
        """Marks `crop` as sent as the input `carrier` at `now`. A crop no
        longer queued (confirmed or dropped) is not tracked."""
        if crop not in self._crops:
            return
        crop.carriers.append(carrier)
        crop.sent_at = now
        self._carried[carrier] = crop

    def next(self, carrier, now):
        """The highest-priority crop not in flight, marked as sent as the
        input `carrier` at `now`; or None if every crop is in flight or there
        are none."""
        best = self.best()
        if best is not None:
            self.sent(best, carrier, now)
        return best

    def on_event(self, event, verifier):
        """Takes an event about a carrier (or a DISCONNECTED or LOST event)
        at any engine; `verifier` is the verifier's engine name. Returns the
        crop the verifier's RESULT confirmed, if any."""
        kind = event.kind
        if kind in _RESET:
            for crop in self._crops:
                crop.sent_at = None
            return None
        crop = self._carried.get(event.id)
        if crop is None:
            return None
        if kind == uriel.EventKind.RESULT and event.engine == verifier:
            self._remove(crop)
            self.confirmed += 1
            logger.info("verifier has the crop of frame %d (score %.3f)", crop.frame_id, crop.score)
            return crop
        ended = kind in _NOT_ACCEPTED or (
            kind == uriel.EventKind.RESULT and event.outcome == uriel.Outcome.DROP
        )
        if ended and crop.carriers[-1] == event.id:
            logger.info(
                "crop of frame %d: %s at %s before the verifier read it; sending it again",
                crop.frame_id,
                kind.name,
                event.engine,
            )
            crop.sent_at = None
        return None

    def discard(self, crop):
        """Removes `crop` without it being confirmed."""
        if crop in self._crops:
            self._remove(crop)

    def _remove(self, crop):
        self._crops.remove(crop)
        for carrier in crop.carriers:
            self._carried.pop(carrier, None)


class Reintegration:
    """The engine's reintegration producer: delivers the crops waiting in
    `queue`, best first, whether or not inputs come in, one at a time: the
    next goes only once the last one's turn in the flow ended (the queue
    settles it on the flow's events; see send_queue.py). Its producer has one
    token, so the flow paces it, and with only one input published at a time
    the client never replaces one before it enters the flow."""

    def __init__(self, queue):
        self.queue = queue
        # The crop published last and its input id, until its turn ends
        self._current = None
        self.published = 0

    def step(self, now, publish):
        """Publishes the best waiting crop with `publish(payload)`, which
        returns its input id, unless the crop published last is still in the
        flow. Returns the crop published, or None."""
        if self._current is not None:
            crop, id = self._current
            if crop in self.queue and crop.in_flight and crop.carriers[-1] == id:
                return None
            self._current = None  # confirmed, dropped, or put back to wait
        best = self.queue.best()
        if best is None:
            return None
        id = publish(best.payload)
        self.queue.sent(best, id, now)
        self._current = (best, id)
        self.published += 1
        logger.info(
            "reintegration: sending the frame of input %d (score %.3f)%s; queue %d",
            best.frame_id,
            best.score,
            f", try {len(best.carriers)}" if len(best.carriers) > 1 else "",
            len(self.queue),
        )
        return best
