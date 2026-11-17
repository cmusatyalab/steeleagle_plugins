"""The prioritized send queue: the engine queues the tiles worth sending,
and the engine's reintegration producer (engine.Reintegration) delivers them,
best first, until the verifier has each. The tile the engine forwards in
place of a camera frame is not a send: in a flow where the engine is a dead
end, it goes nowhere. Tiles the selector must send (like one the verifier
said yes to) go first, then the rest; each group best score first.

Reintegration has one tile in flight at a time (its producer has one token),
as an input with an id the flow's events name (its carrier). Every input
ends in the flow with an event, so no timer is needed:

- the verifier's RESULT: the verifier read the tile (yes or no), which leaves
  the queue;
- SKIPPED or TIMED_OUT at any engine, or a RESULT of another engine that
  dropped it (before the verifier, e.g. an encoder or decoder failing): its
  token is back and the verifier never read it, so it waits to be sent again;
- DISCONNECTED, or LOST (the event that settled it may be among the lost):
  the tile in flight waits to be sent again.

ACCEPTED and SENT leave it in flight: for a remote verifier ACCEPTED means
only that the remote server received it. A tile put back may reach the
verifier twice (if its RESULT was among the lost); that is the price of never
losing one. When the queue is over `max_size`, the tile with the lowest
priority (not a must-send, lowest score) is dropped, in flight or not.
"""
import itertools
import logging
from dataclasses import dataclass, field

import uriel

logger = logging.getLogger("cliped.send_queue")

# Events that end a tile's offer at an engine without it being processed.
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
    # The ids of every input it went out as; the last is the latest
    carriers: list = field(default_factory=list)
    # When it was last sent (time.monotonic()), or None while it waits
    sent_at: float | None = None
    # Its image embedding, to rescore it and to learn from the verifier
    embedding: object = None
    # Whether the selector must send it: it goes ahead of every other tile
    must_send: bool = False
    # The prompt it was cut for (box_rerank counts a prompt switch): an
    # answer about a tile of an earlier prompt isn't learned from
    generation: int = 0

    @property
    def in_flight(self):
        return self.sent_at is not None

    @property
    def priority(self):
        """Higher goes first: must-sends, then score, then the oldest."""
        return (self.must_send, self.score, -self.order)


class SendQueue:
    def __init__(self, *, max_size):
        if max_size < 1:
            raise ValueError("max_size must be at least 1")
        self.max_size = max_size
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
        """Queues `tile`, dropping the lowest-priority tile if the queue is
        over its size. Returns the tile dropped, if any."""
        tile.order = next(self._order)
        self._tiles.append(tile)
        if len(self._tiles) <= self.max_size:
            return None
        # Lowest priority; of equal ones, the newest
        worst = min(self._tiles, key=lambda t: t.priority)
        self._remove(worst)
        logger.debug("queue full; dropped the tile of frame %d (score %.3f)", worst.frame_id, worst.score)
        return worst

    def __contains__(self, tile):
        return tile in self._tiles

    def __iter__(self):
        """The queued tiles, waiting or in flight, in no particular order."""
        return iter(list(self._tiles))

    def best(self):
        """The highest-priority tile not in flight, or None if every tile is in
        flight or there are none."""
        waiting = [tile for tile in self._tiles if not tile.in_flight]
        return max(waiting, key=lambda t: t.priority, default=None)

    def sent(self, tile, carrier, now):
        """Marks `tile` as sent as the input `carrier` at `now`. A tile no
        longer queued (confirmed or dropped) is not tracked."""
        if tile not in self._tiles:
            return
        tile.carriers.append(carrier)
        tile.sent_at = now
        self._carried[carrier] = tile

    def next(self, carrier, now):
        """The highest-priority tile not in flight, marked as sent as the input
        `carrier` at `now`; or None if every tile is in flight or there are
        none."""
        best = self.best()
        if best is not None:
            self.sent(best, carrier, now)
        return best

    def on_event(self, event, verifier):
        """Takes an event about a carrier (or a DISCONNECTED or LOST event)
        at any engine; `verifier` is the verifier's engine name. Returns the
        tile the verifier's RESULT confirmed, if any."""
        kind = event.kind
        if kind in _RESET:
            for tile in self._tiles:
                tile.sent_at = None
            return None
        tile = self._carried.get(event.id)
        if tile is None:
            return None
        if kind == uriel.EventKind.RESULT and event.engine == verifier:
            self._remove(tile)
            self.confirmed += 1
            logger.info("verifier has the tile of frame %d (score %.3f)", tile.frame_id, tile.score)
            return tile
        ended = kind in _NOT_ACCEPTED or (kind == uriel.EventKind.RESULT and event.outcome == uriel.Outcome.DROP)
        if ended and tile.carriers[-1] == event.id:
            logger.info("tile of frame %d: %s at %s before the verifier read it; sending it again",
                        tile.frame_id, kind.name, event.engine)
            tile.sent_at = None
        return None

    def rescore(self, passes):
        """Rescores and reprioritizes every tile with `passes(tile)`, which
        returns whether the tile is still worth sending, its new score, and
        whether it must be sent; and drops each waiting tile that is not
        worth sending. A tile in flight is kept, as the verifier may have it.
        Returns the tiles dropped."""
        dropped = []
        for tile in list(self._tiles):
            keep, tile.score, tile.must_send = passes(tile)
            if not keep and not tile.in_flight:
                self._remove(tile)
                dropped.append(tile)
        return dropped

    def discard(self, tile):
        """Removes `tile` without it being confirmed, e.g. one too large to
        send."""
        if tile in self._tiles:
            self._remove(tile)

    def _remove(self, tile):
        self._tiles.remove(tile)
        for carrier in tile.carriers:
            self._carried.pop(carrier, None)
