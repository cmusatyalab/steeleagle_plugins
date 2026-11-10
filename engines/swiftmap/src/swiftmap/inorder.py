"""Puts numbered frames back in order. Frames after a gap are held until the
missing ones are filled in or, after gap_timeout, given up on."""

RETRY_INTERVAL_S = 10.0
MAX_HELD = 300


def runs(numbers):
    """Sorted `numbers` as runs of consecutive ones, (first, last)."""
    out = []
    for n in numbers:
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


class InOrder:
    def __init__(self, gap_timeout, *, retry_interval=RETRY_INTERVAL_S, max_held=MAX_HELD):
        self.gap_timeout = gap_timeout
        self.retry_interval = retry_interval
        self.max_held = max_held
        self.next = None  # next number to release
        self.highest = None
        self.held = {}  # number -> frame
        self.missing = {}  # number -> when found missing
        self._request_due = False
        self._last_request = float("-inf")
        self.recovered = 0
        self.given_up = 0

    def add(self, number, frame, now):
        """A frame from the flow; returns the frames now ready, in order."""
        ready = []
        if self.highest is not None and number <= self.highest:
            ready = self.flush()  # numbers only go up, so the encoder restarted
        if self.highest is None:
            self.next = number
        elif number > self.highest + 1:
            self.missing.update(dict.fromkeys(range(self.highest + 1, number), now))
            self._request_due = True
        self.highest = number
        self.held[number] = frame
        if len(self.held) > self.max_held:
            ready += self._give_up(list(self.missing))
        return ready + self._release()

    def fill(self, number, frame):
        """A missing frame sent back; returns the frames now ready."""
        if self.missing.pop(number, None) is None:
            return []
        self.held[number] = frame
        self.recovered += 1
        return self._release()

    def expire(self, now):
        """Gives up on frames missing for gap_timeout; returns the frames now ready."""
        return self._give_up([n for n, t in self.missing.items() if now - t >= self.gap_timeout])

    def requests(self, now):
        """Runs of missing numbers to ask for: at once on a new gap, then every
        retry_interval."""
        if not self.missing or (not self._request_due
                                and now - self._last_request < self.retry_interval):
            return []
        self._request_due, self._last_request = False, now
        return runs(sorted(self.missing))

    def flush(self):
        """Gives up on all missing frames and forgets the stream; returns all held."""
        ready = self._give_up(list(self.missing))
        self.next = self.highest = None
        return ready

    def _give_up(self, numbers):
        for n in numbers:
            del self.missing[n]
        self.given_up += len(numbers)
        return self._release()

    def _release(self):
        ready = []
        while self.next is not None and self.next <= self.highest and self.next not in self.missing:
            if self.next in self.held:
                ready.append(self.held.pop(self.next))
            self.next += 1
        return ready
