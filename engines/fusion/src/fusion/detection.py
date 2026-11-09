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

An input still waiting when its timeout ends, counted from when it reaches the
engine, is decided with what there is: the local result, or no result.

Each decided input is logged on one line: the result chosen and why, then what
each engine did with it, e.g.

    input 412     chose: remote (in time)               | local: airplane (0.58)                   | remote +46 ms: robotic dog (0.79)

Every SUMMARY_INTERVAL_NS of decided inputs is also summarized: what was
chosen, what each engine did, and in how many inputs each engine reported
each class.
"""

import logging
from collections import Counter, OrderedDict
from dataclasses import dataclass, field

import uriel
from google.protobuf.message import DecodeError
from steeleagle_protocol.v1.messages.result import result_pb2

logger = logging.getLogger("fusion.detection")

# The most inputs whose events are kept. Events of inputs the engine never
# gets (another flow, or an input skipped on the way to it) are forgotten
# after this many.
MAX_TRACKED = 1024
# The most decided inputs remembered, to log the results that come too late.
MAX_DECIDED = 256
# How often the decided inputs are summarized
SUMMARY_INTERVAL_NS = 30 * 1_000_000_000
# The most classes listed per engine, and paired, in a summary
SUMMARY_CLASSES = 8
SUMMARY_PAIRED_CLASSES = 5
# Widths the columns of an input's line are padded to, so they line up
DECISION_WIDTH = 38
LOCAL_WIDTH = 40

# Why an engine has no result for an input
NO_RESULT = "no result"  # it dropped the input without one
SKIPPED = "skipped"
TIMED_OUT = "timed out"
DISCONNECTED = "disconnected"
LOST = "events lost"
PENDING = "pending"
# The order failures are counted in a summary; the first ones always are
_FAILURES = (NO_RESULT, SKIPPED, TIMED_OUT, DISCONNECTED, LOST, PENDING)
_ALWAYS_COUNTED = 3

_FAILED = {
    uriel.EventKind.SKIPPED: SKIPPED,
    uriel.EventKind.TIMED_OUT: TIMED_OUT,
}


def parse(data):
    """The (class, confidence) of each detection in the serialized
    DetectionResult `data`, highest confidence first, or None if it does not
    decode."""
    result = result_pb2.DetectionResult()
    try:
        result.ParseFromString(data)
    except DecodeError:
        return None
    detections = sorted(result.detections, key=lambda d: d.score, reverse=True)
    return [(d.class_name or "?", d.score) for d in detections]


def summarize(data):
    """The detections in the serialized DetectionResult `data`, as
    `class (confidence)`, highest confidence first."""
    detections = parse(data)
    if detections is None:
        return f"undecodable result ({len(data)} bytes)"
    if not detections:
        return "0 detections"
    return ", ".join(f"{name} ({score:.2f})" for name, score in detections)


def _failure_reason(engine_name, why):
    """Why the result of the other engine was chosen, or none was, given that
    `engine_name` ("local" or "remote") failed with `why`."""
    return (
        f"{engine_name} had no result" if why == NO_RESULT else f"{engine_name} {why}"
    )


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
    # Why each failed engine has no result
    failed: dict = field(default_factory=dict)

    def fail(self, engine, why):
        if engine not in self.results:
            self.failed[engine] = why

    def status(self, engine):
        """What `engine` did with the input: its detections, or why it has
        none."""
        result = self.results.get(engine)
        if result is not None:
            return summarize(result.data)
        return self.failed.get(engine, PENDING)


@dataclass(frozen=True)
class Wait:
    """The input is not decided yet; `deadline_ns` is when it will be, if
    nothing else happens first."""

    deadline_ns: int


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


class Summary:
    """Counts of the inputs decided since the last summary."""

    def __init__(self, started_ns):
        self.started_ns = started_ns
        self.inputs = 0
        self.chosen = Counter()
        # Per engine name: results, and failures by why
        self.outcomes = {"local": Counter(), "remote": Counter()}
        # Per engine name: inputs it reported each class in, and inputs it
        # reported no detections in
        self.classes = {"local": Counter(), "remote": Counter()}
        self.empty = Counter()
        # Per class: inputs where both, only remote, or only local reported it
        self.paired = {}

    def add(self, chosen, classes, failures):
        """Counts an input: the engine name `chosen` (or None), the set of
        classes each engine name reported (absent if it had no result, None
        if its result did not decode), and
        why each engine name without a result had none."""
        self.inputs += 1
        self.chosen[chosen or "none"] += 1
        for name, why in failures.items():
            self.outcomes[name][why] += 1
        for name, found in classes.items():
            self.outcomes[name]["results"] += 1
            if found is None:  # undecodable
                continue
            self.classes[name].update(found)
            if not found:
                self.empty[name] += 1
        local = classes.get("local") or set()
        remote = classes.get("remote") or set()
        for name in local | remote:
            both, remote_only, local_only = self.paired.get(name, (0, 0, 0))
            self.paired[name] = (
                both + (name in local and name in remote),
                remote_only + (name in remote and name not in local),
                local_only + (name in local and name not in remote),
            )

    def lines(self, now_ns):
        seconds = (now_ns - self.started_ns) / 1e9
        chosen = ", ".join(
            f"{name} {self.chosen[name]}" for name in ("remote", "local", "none")
        )
        lines = [f"last {seconds:.0f} s: {self.inputs} inputs | chose {chosen}"]
        for name in ("local", "remote"):
            outcomes = self.outcomes[name]
            counts = [f"{outcomes['results']} results"] + [
                f"{outcomes[why]} {why}"
                for i, why in enumerate(_FAILURES)
                if i < _ALWAYS_COUNTED or outcomes[why]
            ]
            found = [
                f"{cls} ({n})"
                for cls, n in self.classes[name].most_common(SUMMARY_CLASSES)
            ]
            if len(self.classes[name]) > SUMMARY_CLASSES:
                found.append(f"{len(self.classes[name]) - SUMMARY_CLASSES} more")
            if self.empty[name]:
                found.append(f"0 detections ({self.empty[name]})")
            lines.append(
                f"  {name + ':':<7} {', '.join(counts)}"
                + (f" | {', '.join(found)}" if found else "")
            )
        if self.paired:
            top = sorted(self.paired.items(), key=lambda item: -sum(item[1]))
            lines.append(
                "  inputs with class, both / remote only / local only: "
                + ", ".join(
                    f"{cls} {both}/{remote_only}/{local_only}"
                    for cls, (both, remote_only, local_only) in top[
                        :SUMMARY_PAIRED_CLASSES
                    ]
                )
            )
        return lines


class DetectionFusion:
    def __init__(
        self,
        *,
        local,
        remote,
        grace_ns,
        timeout_ns,
        clock,
        summary_interval_ns=SUMMARY_INTERVAL_NS,
    ):
        if local == remote:
            raise ValueError(f"the local and remote engines are both {local!r}")
        self.local = local
        self.remote = remote
        self.grace_ns = grace_ns
        self.timeout_ns = timeout_ns
        self.clock = clock
        self.summary_interval_ns = summary_interval_ns
        self._summary = None
        self._inputs = OrderedDict()
        self._disconnected = set()
        # The input being decided, which is never forgotten for MAX_TRACKED
        self._current = None
        # When the input being decided times out
        self._timeout_at_ns = None
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
            self._fail_all(engine, DISCONNECTED)
            return
        if kind == uriel.EventKind.LOST:
            logger.warning("%d events of %s were lost", event.lost, engine)
            self._fail_all(engine, LOST)
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
                    "input %d: %s accepted too late, %.1f ms after returning %s [%s]",
                    event.id.seq,
                    self._name(engine),
                    (self.clock() - decided.at_ns) / 1e6,
                    decided.name or "no result",
                    summarize(event.result),
                )
            return
        if kind == uriel.EventKind.RESULT:
            if failed:
                self._outcomes(event.id).fail(engine, NO_RESULT)
            else:
                outcomes = self._outcomes(event.id)
                outcomes.failed.pop(engine, None)
                outcomes.results[engine] = Result(
                    event.result, event.payload_type, self.clock()
                )
        elif kind in _FAILED:
            self._outcomes(event.id).fail(engine, _FAILED[kind])

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

    def _fail_all(self, engine, why):
        for outcomes in self._inputs.values():
            outcomes.fail(engine, why)

    def _name(self, engine):
        return "local" if engine == self.local else "remote"

    # Inputs

    def begin(self, id):
        """Starts deciding the input `id`. An engine that is disconnected
        will not report on it."""
        self._current = id
        self._timeout_at_ns = self.clock() + self.timeout_ns
        outcomes = self._outcomes(id)
        for engine in self._disconnected:
            outcomes.fail(engine, DISCONNECTED)

    def decide(self, id):
        """What to do with the input `id` now: a Wait or a Report."""
        outcomes = self._outcomes(id)
        local = outcomes.results.get(self.local)
        remote = outcomes.results.get(self.remote)
        now = self.clock()
        timed_out = now >= self._timeout_at_ns
        if local is not None:
            deadline = local.at_ns + self.grace_ns
            if remote is not None and remote.at_ns <= deadline:
                return self._report(id, outcomes, self.remote, "in time")
            if remote is not None:
                return self._report(id, outcomes, self.local, "remote was late")
            if self.remote in outcomes.failed:
                reason = _failure_reason("remote", outcomes.failed[self.remote])
                return self._report(id, outcomes, self.local, reason)
            if now >= deadline:
                return self._report(id, outcomes, self.local, "grace period ended")
            if timed_out:
                return self._report(id, outcomes, self.local, "timed out")
            return Wait(min(deadline, self._timeout_at_ns))
        if remote is not None:
            reason = _failure_reason("local", outcomes.failed.get(self.local, PENDING))
            return self._report(id, outcomes, self.remote, reason)
        if self.local in outcomes.failed and self.remote in outcomes.failed:
            return self._report(id, outcomes, None, "neither engine accepted")
        if timed_out:
            reason = f"timed out after {self.timeout_ns // 1_000_000} ms"
            return self._report(id, outcomes, None, reason)
        return Wait(self._timeout_at_ns)

    def _report(self, id, outcomes, source, reason):
        """The Report of the input `id`, with the result of the engine
        `source` (None for no result), which is logged and remembered."""
        name = None if source is None else self._name(source)
        self._log_decided(id, outcomes, name, reason)
        self._count(outcomes, name)
        self._decided[id] = Decided(name, self.clock())
        self._decided.move_to_end(id)
        while len(self._decided) > MAX_DECIDED:
            self._decided.popitem(last=False)
        if source is None:
            return Report()
        return Report(outcomes.results[source], source)

    def _log_decided(self, id, outcomes, name, reason):
        local = outcomes.results.get(self.local)
        remote = outcomes.results.get(self.remote)
        timing = ""
        if local is not None and remote is not None:
            timing = f" {(remote.at_ns - local.at_ns) / 1e6:+.0f} ms"
        chose = f"chose: {name or 'none'} ({reason})"
        logger.info(
            "input %-7d %s| %s | remote%s: %s",
            id.seq,
            chose.ljust(DECISION_WIDTH),
            f"local: {outcomes.status(self.local)}".ljust(LOCAL_WIDTH),
            timing,
            outcomes.status(self.remote),
        )

    def _count(self, outcomes, chosen):
        """Counts the decided input in the summary, and logs the summary if
        it is due."""
        now = self.clock()
        if self._summary is None:
            self._summary = Summary(now)
        classes, failures = {}, {}
        for engine in (self.local, self.remote):
            name = self._name(engine)
            result = outcomes.results.get(engine)
            if result is None:
                failures[name] = outcomes.failed.get(engine, PENDING)
            else:
                detections = parse(result.data)
                classes[name] = (
                    None if detections is None else {cls for cls, _ in detections}
                )
        self._summary.add(chosen, classes, failures)
        if now - self._summary.started_ns >= self.summary_interval_ns:
            for line in self._summary.lines(now):
                logger.info("%s", line)
            self._summary = Summary(now)

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
