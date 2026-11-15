"""The prioritized send queue: crops wait in it until the verifier has them,
so a crop sent while the link is down is not lost. The crop of the most
confident detection goes first.

A crop goes out in two ways, each as an input with an id the verifier's
events name (its carrier): in place of the camera frame it was cut from, by
the engine, and on the engine's reintegration producer, which sends the
queue's waiting crops best first (engine.Reintegration). A crop leaves the
queue only on the verifier's RESULT event for one of its carriers: the
verifier finished it.

ACCEPTED is not enough. For a remote verifier it means only that the remote
server received the crop; if the verifier then never takes it (it is busy or
stuck), the events are ACCEPTED, SENT, TIMED_OUT. (A local verifier's events
have no ACCEPTED at all.) So ACCEPTED and SENT leave a crop in flight. Until
its RESULT:

- SKIPPED or TIMED_OUT for its latest carrier puts it back to wait;
- DISCONNECTED, or LOST (the RESULT may be among the lost events), puts
  every crop in flight back to wait;
- a crop in flight longer than `resend_after_s` with no word goes back to
  wait, in case it was dropped before it reached the verifier at all, e.g. by
  an encoder between them.

A crop put back may be sent again and reach the verifier twice; that is the
price of never losing one. When the queue is over `max_size`, the crop with
the lowest score is dropped, in flight or not.
"""

import itertools
import logging
from dataclasses import dataclass, field

import uriel

logger = logging.getLogger("detection_crop.send_queue")

# The event that shows the verifier has the crop.
_CONFIRMED = frozenset({uriel.EventKind.RESULT})
# Events that end one offer of a crop without it reaching the verifier.
_NOT_ACCEPTED = frozenset({uriel.EventKind.SKIPPED, uriel.EventKind.TIMED_OUT})
# Events after which no crop in flight can be counted on.
_RESET = frozenset({uriel.EventKind.DISCONNECTED, uriel.EventKind.LOST})


@dataclass(eq=False)
class Crop:
    """A crop waiting for the verifier."""

    # The confidence of the detection it was cropped to
    score: float
    # The serialized RawFrame of the crop, as it is sent
    payload: bytes
    # The id of the frame it was cut from (the RawFrame's id), for logs
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
