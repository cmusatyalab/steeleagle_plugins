"""The prioritized send queue: tiles wait in it, best score first, until the
verifier has them, so a tile sent while the link is down is not lost.

An engine can only send something as the outcome of an input, so a tile goes
out in place of an input (its carrier), and keeps that input's id: the
verifier's events name the carrier. Each input carries the best tile that is
not already in flight. A tile leaves the queue only on the verifier's RESULT
event for a carrier it went out as: the verifier finished it.

ACCEPTED is not enough. For a remote verifier it means only that the remote
server received the tile; if the verifier then never takes it (it is busy or
stuck), the events are ACCEPTED, SENT, TIMED_OUT. (A local verifier's events
have no ACCEPTED at all.) So ACCEPTED and SENT leave a tile in flight. Until
its RESULT:

- SKIPPED or TIMED_OUT for its latest carrier puts it back to wait;
- DISCONNECTED, or LOST (the ACCEPTED may be among the lost events), puts
  every tile in flight back to wait;
- a tile in flight longer than `resend_after_s` with no word goes back to
  wait, in case it was dropped before it reached the verifier at all, e.g. by
  the encoder between them.

A tile put back may be sent again and reach the verifier twice; that is the
price of never losing one. When the queue is over `max_size`, the tile with the
lowest score is dropped, in flight or not.
"""
import itertools
import logging
from dataclasses import dataclass, field

import uriel

logger = logging.getLogger("cliped.send_queue")

# The event that shows the verifier has the tile.
_CONFIRMED = frozenset({uriel.EventKind.RESULT})
# Events that end one offer of a tile without it reaching the verifier.
_NOT_ACCEPTED = frozenset({uriel.EventKind.SKIPPED, uriel.EventKind.TIMED_OUT})
# Events after which no tile in flight can be counted on.
_RESET = frozenset({uriel.EventKind.DISCONNECTED, uriel.EventKind.LOST})


@dataclass(eq=False)
class Tile:
    """A tile waiting for the verifier."""

    score: float
    # The serialized RawFrame of the tile, as it is sent
    payload: bytes
    # The id of the frame it was cut from, and its box in that frame
    frame_id: int
    box: tuple
    # Its number among the tiles of that frame
    index: int = 0
    order: int = 0
    # The ids of every input it went out in place of; the last is the latest
    carriers: list = field(default_factory=list)
    # When it was last sent (time.monotonic()), or None while it waits
    sent_at: float | None = None

    @property
    def in_flight(self):
        return self.sent_at is not None


class SendQueue:
    def __init__(self, *, max_size, resend_after_s):
        if max_size < 1:
            raise ValueError("max_size must be at least 1")
        self.max_size = max_size
        self.resend_after_s = resend_after_s
        self._tiles = []
        # Carrier id -> the tile it carried
        self._carried = {}
        self._order = itertools.count()
        # How many tiles the verifier confirmed
        self.confirmed = 0

    def __len__(self):
        return len(self._tiles)

    @property
    def in_flight(self):
        return sum(tile.in_flight for tile in self._tiles)

    def push(self, tile):
        """Queues `tile`, dropping the lowest-scored tile if the queue is over
        its size. Returns the tile dropped, if any."""
        tile.order = next(self._order)
        self._tiles.append(tile)
        if len(self._tiles) <= self.max_size:
            return None
        # Lowest score; of equal scores, the newest
        worst = min(self._tiles, key=lambda t: (t.score, -t.order))
        self._remove(worst)
        logger.debug("queue full; dropped the tile of frame %d (score %.3f)", worst.frame_id, worst.score)
        return worst

    def next(self, carrier, now):
        """The best tile not in flight, marked as sent in place of the input
        `carrier` at `now`; or None if every tile is in flight or there are
        none."""
        best = None
        for tile in self._tiles:
            if tile.in_flight and now - tile.sent_at >= self.resend_after_s:
                logger.info("tile of frame %d unacknowledged for %.1fs; resending it", tile.frame_id, now - tile.sent_at)
                tile.sent_at = None
            if not tile.in_flight and (best is None or (tile.score, -tile.order) > (best.score, -best.order)):
                best = tile
        if best is not None:
            best.carriers.append(carrier)
            best.sent_at = now
            self._carried[carrier] = best
        return best

    def on_event(self, event):
        """Takes an event of the verifier."""
        kind = event.kind
        if kind in _RESET:
            for tile in self._tiles:
                tile.sent_at = None
            return
        tile = self._carried.get(event.id)
        if tile is None:
            return
        if kind in _CONFIRMED:
            self._remove(tile)
            self.confirmed += 1
            logger.info("verifier has the tile of frame %d (score %.3f)", tile.frame_id, tile.score)
        elif kind in _NOT_ACCEPTED and tile.carriers[-1] == event.id:
            tile.sent_at = None

    def discard(self, tile):
        """Removes `tile` without it being confirmed, e.g. one too large to
        send."""
        if tile in self._tiles:
            self._remove(tile)

    def _remove(self, tile):
        self._tiles.remove(tile)
        for carrier in tile.carriers:
            self._carried.pop(carrier, None)
