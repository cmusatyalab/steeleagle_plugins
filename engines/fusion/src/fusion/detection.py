"""Detection fusion: for each frame, the result of the remote engine if it
arrives in time, the result of the local engine otherwise.

The engine subscribes to the events of both engines and records, for each
input, what each engine did with it:

- a result: a RESULT event, unless the engine dropped the input with no
  result;
- a failure: a RESULT event that dropped the input with no result, or a
  SKIPPED or TIMED_OUT event. A DISCONNECTED event fails every input the
  engine has not finished, and every input that arrives until the engine is
  heard from again; a LOST event fails every input the engine has not
  finished, since their results may be among the lost events (a result that
  still arrives before the input is decided is used).

An input is then decided by:

| remote \\ local | result                        | failure | pending |
| --------------- | ----------------------------- | ------- | ------- |
| result          | remote, if it arrived by the end of the grace period; local otherwise | remote | remote |
| failure         | local                         | drop    | wait    |
| pending         | wait until the grace period ends, then local | wait | wait |

The grace period starts when the engine receives the local result.
"""

import logging
from collections import OrderedDict
from dataclasses import dataclass, field

import uriel

logger = logging.getLogger("fusion.detection")

# The most inputs whose events are kept. Events of inputs the engine never
# gets (another flow, or an input skipped on the way to it) are forgotten
# after this many.
MAX_TRACKED = 1024

_FAILED = frozenset({uriel.EventKind.SKIPPED, uriel.EventKind.TIMED_OUT})


@dataclass(frozen=True)
class Result:
    """An engine's result, and when the fusion engine received it."""

    data: bytes
    payload_type: str
    at_ns: int


@dataclass
class Outcomes:
    """What each engine did with one input. An engine absent from both is
    pending."""

    results: dict = field(default_factory=dict)
    failed: set = field(default_factory=set)

    def fail(self, engine):
        if engine not in self.results:
            self.failed.add(engine)


@dataclass(frozen=True)
class Wait:
    """The input is not decided yet; `deadline_ns` is when it will be, if
    nothing else happens first, or None if only an event can decide it."""

    deadline_ns: int | None = None


@dataclass(frozen=True)
class Report:
    """The input is decided: report `result` (None to drop it with no
    result), which came from the engine `source`."""

    result: Result | None = None
    source: str | None = None


class DetectionFusion:
    def __init__(self, *, local, remote, grace_ns, clock):
        if local == remote:
            raise ValueError(f"the local and remote engines are both {local!r}")
        self.local = local
        self.remote = remote
        self.grace_ns = grace_ns
        self.clock = clock
        self._inputs = OrderedDict()
        self._disconnected = set()
        # The input being decided, which is never forgotten for MAX_TRACKED
        self._current = None

    # Events

    def on_event(self, event):
        engine = event.engine
        if engine not in (self.local, self.remote):
            return
        kind = event.kind
        if kind == uriel.EventKind.DISCONNECTED:
            logger.info("%s disconnected", engine)
            self._disconnected.add(engine)
            self._fail_all(engine)
            return
        if kind == uriel.EventKind.LOST:
            logger.warning("%d events of %s were lost", event.lost, engine)
            self._fail_all(engine)
            return
        if engine in self._disconnected:
            logger.info("%s is reachable again", engine)
            self._disconnected.discard(engine)
        if kind == uriel.EventKind.RESULT:
            if event.outcome == uriel.Outcome.DROP and not event.result:
                self._outcomes(event.id).fail(engine)
            else:
                outcomes = self._outcomes(event.id)
                outcomes.failed.discard(engine)
                outcomes.results[engine] = Result(
                    event.result, event.payload_type, self.clock()
                )
        elif kind in _FAILED:
            self._outcomes(event.id).fail(engine)

    def _outcomes(self, id):
        outcomes = self._inputs.get(id)
        if outcomes is None:
            outcomes = self._inputs[id] = Outcomes()
            while len(self._inputs) > MAX_TRACKED:
                oldest = next(iter(self._inputs))
                if oldest == self._current:
                    self._inputs.move_to_end(oldest)
                else:
                    del self._inputs[oldest]
        return outcomes

    def _fail_all(self, engine):
        for outcomes in self._inputs.values():
            outcomes.fail(engine)

    # Inputs

    def begin(self, id):
        """Starts deciding the input `id`. An engine that is disconnected
        will not report on it."""
        self._current = id
        outcomes = self._outcomes(id)
        for engine in self._disconnected:
            outcomes.fail(engine)

    def decide(self, id):
        """What to do with the input `id` now: a Wait or a Report."""
        outcomes = self._outcomes(id)
        local = outcomes.results.get(self.local)
        remote = outcomes.results.get(self.remote)
        if local is not None:
            deadline = local.at_ns + self.grace_ns
            if remote is not None and remote.at_ns <= deadline:
                return Report(remote, self.remote)
            if remote is not None or self.remote in outcomes.failed:
                return Report(local, self.local)
            if self.clock() >= deadline:
                return Report(local, self.local)
            return Wait(deadline)
        if remote is not None:
            return Report(remote, self.remote)
        if self.local in outcomes.failed and self.remote in outcomes.failed:
            return Report()
        return Wait()

    def end(self, id):
        """Forgets the input `id`, and every earlier input of its producer's
        session, which the engine will not get."""
        if self._current == id:
            self._current = None
        self._inputs.pop(id, None)
        for other in [
            other
            for other in self._inputs
            if other.producer == id.producer
            and other.session == id.session
            and other.seq < id.seq
        ]:
            del self._inputs[other]

    @property
    def tracked(self):
        """How many inputs' events are kept."""
        return len(self._inputs)
