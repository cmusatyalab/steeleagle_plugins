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
# The most decided inputs remembered, to log the results that come too late.
MAX_DECIDED = 256

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
    # The engines whose results were logged
    logged: set = field(default_factory=set)

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


@dataclass(frozen=True)
class Decided:
    """What was reported for an input: the result of the engine `name`
    ("local" or "remote", or None for no result), at the time `at_ns`."""

    name: str | None
    at_ns: int


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
        self._decided = OrderedDict()

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
        failed = event.outcome == uriel.Outcome.DROP and not event.result
        decided = self._decided.get(event.id)
        if decided is not None:
            # It can no longer change what was reported
            if kind == uriel.EventKind.RESULT and not failed:
                logger.info(
                    "input %d: %s accepted too late, %.1f ms after returning %s",
                    event.id.seq,
                    self._name(engine),
                    (self.clock() - decided.at_ns) / 1e6,
                    decided.name or "no result",
                )
            return
        if kind == uriel.EventKind.RESULT:
            if failed:
                self._outcomes(event.id).fail(engine)
            else:
                outcomes = self._outcomes(event.id)
                outcomes.failed.discard(engine)
                outcomes.results[engine] = Result(
                    event.result, event.payload_type, self.clock()
                )
                self._log_accepted(event.id, outcomes)
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

    def _name(self, engine):
        return "local" if engine == self.local else "remote"

    def _log_accepted(self, id, outcomes):
        """Logs the results of the input being decided that are not logged
        yet. An engine also finishes inputs that never reach this engine,
        which are not logged."""
        if id != self._current:
            return
        unlogged = [e for e in outcomes.results if e not in outcomes.logged]
        for engine in sorted(unlogged, key=lambda e: outcomes.results[e].at_ns):
            other = self.remote if engine == self.local else self.local
            if other in outcomes.logged:
                after = outcomes.results[engine].at_ns - outcomes.results[other].at_ns
                logger.info(
                    "input %d: %s accepted, %.1f ms after %s",
                    id.seq,
                    self._name(engine),
                    after / 1e6,
                    self._name(other),
                )
            else:
                logger.info(
                    "input %d: %s accepted first, waiting", id.seq, self._name(engine)
                )
            outcomes.logged.add(engine)

    # Inputs

    def begin(self, id):
        """Starts deciding the input `id`. An engine that is disconnected
        will not report on it."""
        self._current = id
        outcomes = self._outcomes(id)
        for engine in self._disconnected:
            outcomes.fail(engine)
        self._log_accepted(id, outcomes)

    def decide(self, id):
        """What to do with the input `id` now: a Wait or a Report."""
        outcomes = self._outcomes(id)
        local = outcomes.results.get(self.local)
        remote = outcomes.results.get(self.remote)
        if local is not None:
            deadline = local.at_ns + self.grace_ns
            if remote is not None and remote.at_ns <= deadline:
                return self._report(id, remote, self.remote, "in time")
            if remote is not None:
                return self._report(id, local, self.local, "remote was late")
            if self.remote in outcomes.failed:
                return self._report(id, local, self.local, "remote failed")
            if self.clock() >= deadline:
                return self._report(id, local, self.local, "grace period ended")
            return Wait(deadline)
        if remote is not None:
            reason = "local failed" if self.local in outcomes.failed else "local pending"
            return self._report(id, remote, self.remote, reason)
        if self.local in outcomes.failed and self.remote in outcomes.failed:
            logger.info("input %d: neither engine accepted, returning no result", id.seq)
            return self._report(id)
        return Wait()

    def _report(self, id, result=None, source=None, reason=None):
        """The Report of the input `id`, which is logged and remembered."""
        name = None
        if source is not None:
            name = self._name(source)
            logger.info("input %d: returning %s result (%s)", id.seq, name, reason)
        self._decided[id] = Decided(name, self.clock())
        self._decided.move_to_end(id)
        while len(self._decided) > MAX_DECIDED:
            self._decided.popitem(last=False)
        return Report(result, source)

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
