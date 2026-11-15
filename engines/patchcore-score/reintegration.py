"""Prioritized reintegration of anomalous frames, as detection-crop does for
its crops (vendored from detection-crop's send_queue.py and engine.py, with
crops as frames).

Each anomalous frame forwarded is queued too, scored by how far its anomaly
score is above the threshold, until the verifier's RESULT event for it shows
the verifier finished it. (Any engine that finishes the frame posts a RESULT,
even with no result, so the verifier can be the decoder at the far end.) The
engine's reintegration producer sends the queue's waiting frames, best first,
whenever the flow has room for them, so a frame lost, timed out, or found
while the link was down still reaches the verifier.

A frame goes out in two ways, each as an input with an id the verifier's
events name (its carrier): in place of the input it was scored from, by the
engine, and on the reintegration producer. Until its RESULT:

- SKIPPED or TIMED_OUT for its latest carrier puts it back to wait;
- DISCONNECTED, or LOST, puts every frame in flight back to wait;
- a frame in flight longer than `resend_after_s` with no word goes back to
  wait.

A frame put back may reach the verifier twice; that is the price of never
losing one. Past `max_size`, the frame with the lowest score is dropped.
"""

import itertools
import logging
from dataclasses import dataclass, field

import uriel

logger = logging.getLogger("patchcore_score.reintegration")

# The event that shows the verifier has the crop.
_CONFIRMED = frozenset({uriel.EventKind.RESULT})
# Events that end one offer of a crop without it reaching the verifier.
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
    def __init__(self, *, max_size, resend_after_s):
        if max_size < 1:
            raise ValueError("max_size must be at least 1")
        self.max_size = max_size
        self.resend_after_s = resend_after_s
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

    def best(self, now):
        """The highest-priority crop not in flight, or None if every crop is
        in flight or there are none. Crops in flight for `resend_after_s` (at
        `now`) with no word go back to wait first."""
        best = None
        for crop in self._crops:
            if crop.in_flight and now - crop.sent_at >= self.resend_after_s:
                logger.info(
                    "crop of frame %d unacknowledged for %.1fs; resending it",
                    crop.frame_id,
                    now - crop.sent_at,
                )
                crop.sent_at = None
            if not crop.in_flight and (best is None or crop.priority > best.priority):
                best = crop
        return best

    def sent(self, crop, carrier, now):
        """Marks `crop` as sent as the input `carrier` at `now`. A crop no
        longer queued (confirmed or dropped) is not tracked."""
        if crop not in self._crops:
            return
        crop.carriers.append(carrier)
        crop.sent_at = now
        self._carried[carrier] = crop

    def unsend(self, crop, carrier):
        """Puts `crop` back to wait if its latest carrier is `carrier`, which
        will never reach the verifier, e.g. one replaced before it left."""
        if crop in self._crops and crop.carriers and crop.carriers[-1] == carrier:
            crop.sent_at = None

    def next(self, carrier, now):
        """The highest-priority crop not in flight, marked as sent as the
        input `carrier` at `now`; or None if every crop is in flight or there
        are none."""
        best = self.best(now)
        if best is not None:
            self.sent(best, carrier, now)
        return best

    def on_event(self, event):
        """Takes an event of the verifier. Returns the crop its RESULT
        confirmed, if any."""
        kind = event.kind
        if kind in _RESET:
            for crop in self._crops:
                crop.sent_at = None
            return None
        crop = self._carried.get(event.id)
        if crop is None:
            return None
        if kind in _CONFIRMED:
            self._remove(crop)
            self.confirmed += 1
            logger.info("verifier has the crop of frame %d (score %.3f)", crop.frame_id, crop.score)
            return crop
        if kind in _NOT_ACCEPTED and crop.carriers[-1] == event.id:
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
    """The engine's reintegration producer: sends the crops waiting in
    `queue`, best first, whether or not inputs come in.

    Uriel's client keeps only a producer's newest input until the flow has
    room for it, and an input it replaces there never enters the flow, with no
    event. So one crop at a time waits to be admitted: the next goes once an
    event (`on_event`) shows the last one entered the flow, unless a better
    crop turns up, which replaces it, and it waits again. A crop admitted is
    in flight like any other until the verifier's events settle it
    (send_queue.py)."""

    def __init__(self, queue):
        self.queue = queue
        # The crop published last and its input id, until admitted
        self._pending = None
        self.published = 0

    def on_event(self, event):
        """Takes an event about the producer's inputs, at any engine, or of
        the verifier: any event about the crop waiting means it was
        admitted."""
        if self._pending is not None and event.id == self._pending[1]:
            self._pending = None

    def step(self, now, publish):
        """Publishes the best waiting crop with `publish(payload)`, which
        returns its input id, unless the crop published last still waits to
        be admitted and is no worse. Returns the crop published, or None."""
        best = self.queue.best(now)
        if self._pending is not None:
            crop, id = self._pending
            if crop not in self.queue or not crop.in_flight or crop.carriers[-1] != id:
                self._pending = None  # confirmed, dropped, or put back to wait
            elif best is None or best.priority <= crop.priority:
                return None
            else:
                # The better crop replaces it at the client, so it never leaves
                self.queue.unsend(crop, id)
                self._pending = None
                best = self.queue.best(now)
        if best is None:
            return None
        id = publish(best.payload)
        self.queue.sent(best, id, now)
        self._pending = (best, id)
        self.published += 1
        logger.info(
            "reintegration: sending the frame of input %d (score %.3f); queue %d (%d in flight)",
            best.frame_id,
            best.score,
            len(self.queue),
            self.queue.in_flight,
        )
        return best
