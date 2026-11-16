"""COP (correctable operation) tracking: reports the local engine's detections
while they agree with the remote engine's, and the remote engine's while they
do not.

Like detection fusion, the engine subscribes to the events of both engines and
records what each did with each input: an answer (a RESULT event, which may
have dropped the input with no result, meaning nothing was detected), or a
failure (SKIPPED, TIMED_OUT, DISCONNECTED or LOST).

The engine is in one of three modes:

- local-checked: each input is reported with the local answer as soon as it
  arrives. When the remote answer for the same input arrives within the grace
  period after the local one, the two are checked against each other. If they
  disagree, the engine goes to remote-only.
- remote-only: each input is reported with the remote answer as soon as it
  arrives, if it arrives within the grace period after the local answer (or
  before the local answer). If the grace period ends first, the local answer
  is reported and the remote engine is unreachable. The two are
  still checked against each other, and as soon as they agree the engine goes
  back to local-checked.
- local-only: the remote engine is unreachable, and each input is reported
  with the local answer as soon as it arrives. The first remote answer that
  arrives in time takes the engine to local-checked (and is then checked).

The remote engine is unreachable when it disconnects, times out on an input,
or answers an input after the grace period that started with the local answer
(or has not answered when it ends, in remote-only). A remote engine that
skips an input, because it was still busy with an earlier one, is not
unreachable; in remote-only the input then falls back to the local answer.

A check compares the highest-confidence detection of the tracked class in each
answer. They agree if both have one and the intersection over union of their
boxes is at least the overlap threshold, and disagree if their IoU is lower or
only one has one. If neither has one, the check is inconclusive and the mode
does not change.

The engine starts in local-only, until it hears from the remote engine.
"""

import logging
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from enum import StrEnum

import uriel
from google.protobuf.message import DecodeError
from steeleagle_protocol.v1.messages.result import result_pb2

from fusion.detection import (
    DECISION_WIDTH,
    DISCONNECTED,
    LOCAL_WIDTH,
    LOST,
    NO_RESULT,
    PENDING,
    SKIPPED,
    SUMMARY_INTERVAL_NS,
    TIMED_OUT,
    Report,
    Result,
    Wait,
    summarize,
)

logger = logging.getLogger("fusion.cop")

# The most inputs whose events are kept, decided or not. Inputs the engine
# never gets are still checked, so they are kept until this many newer ones
# push them out.
MAX_TRACKED = 1024

_FAILED = {
    uriel.EventKind.SKIPPED: SKIPPED,
    uriel.EventKind.TIMED_OUT: TIMED_OUT,
}


class Mode(StrEnum):
    LOCAL_CHECKED = "local-checked"
    REMOTE_ONLY = "remote-only"
    LOCAL_ONLY = "local-only"


class Verdict(StrEnum):
    AGREE = "agree"
    DISAGREE = "disagree"
    INCONCLUSIVE = "inconclusive"


def iou(a, b):
    """The intersection over union of the boxes `a` and `b`, each
    (x_min, y_min, x_max, y_max)."""
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    intersection = width * height
    union = (
        (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    )
    return intersection / union if union > 0 else 0.0


def best(data, target):
    """The (confidence, box) of the highest-confidence detection of the class
    `target` (compared case-insensitively) in the serialized DetectionResult
    `data`; None if there is none, or if `data` is None (no result). Raises
    DecodeError if `data` does not decode."""
    if data is None:
        return None
    result = result_pb2.DetectionResult()
    result.ParseFromString(data)
    found = None
    for detection in result.detections:
        if detection.class_name.casefold() != target:
            continue
        if found is None or detection.score > found[0]:
            box = detection.bbox
            found = (detection.score, (box.x_min, box.y_min, box.x_max, box.y_max))
    return found


@dataclass(frozen=True)
class Check:
    verdict: Verdict
    why: str


def check(local_data, remote_data, target, threshold):
    """Whether the local and remote answers (serialized DetectionResults, or
    None for no result) agree on the class `target`."""
    try:
        local = best(local_data, target)
        remote = best(remote_data, target)
    except DecodeError:
        return Check(Verdict.INCONCLUSIVE, "undecodable result")
    if local is None and remote is None:
        return Check(Verdict.INCONCLUSIVE, f"neither found {target}")
    if local is None:
        return Check(Verdict.DISAGREE, f"only remote found {target}")
    if remote is None:
        return Check(Verdict.DISAGREE, f"only local found {target}")
    overlap = iou(local[1], remote[1])
    if overlap >= threshold:
        return Check(Verdict.AGREE, f"IoU {overlap:.2f} >= {threshold:.2f}")
    return Check(Verdict.DISAGREE, f"IoU {overlap:.2f} < {threshold:.2f}")


@dataclass
class Track:
    """What each engine did with one input. An answer is a Result whose data
    is None if the engine dropped the input with no result. An engine absent
    from both `answers` and `failed` is pending."""

    answers: dict = field(default_factory=dict)
    failed: dict = field(default_factory=dict)
    checked: bool = False
    decided: bool = False

    def fail(self, engine, why):
        if engine not in self.answers:
            self.failed[engine] = why

    def settled(self):
        return len(self.answers) + len(self.failed) == 2

    def status(self, engine):
        answer = self.answers.get(engine)
        if answer is not None:
            return NO_RESULT if answer.data is None else summarize(answer.data)
        return self.failed.get(engine, PENDING)


class Summary:
    """Counts since the last summary."""

    def __init__(self, started_ns):
        self.started_ns = started_ns
        self.inputs = 0
        self.chosen = Counter()
        self.modes = Counter()
        self.checks = Counter()
        self.switches = Counter()

    def lines(self, now_ns):
        seconds = (now_ns - self.started_ns) / 1e9

        def counts(counter, keys):
            return ", ".join(f"{key} {counter[key]}" for key in keys)

        lines = [
            f"last {seconds:.0f} s: {self.inputs} inputs"
            f" | chose {counts(self.chosen, ('local', 'remote', 'none'))}"
            f" | in mode {counts(self.modes, list(Mode))}",
            f"  checks: {counts(self.checks, list(Verdict))}",
        ]
        if self.switches:
            lines.append(
                "  switches: "
                + ", ".join(f"{a} -> {b} ({n})" for (a, b), n in self.switches.items())
            )
        return lines


class CopTracking:
    def __init__(
        self,
        *,
        local,
        remote,
        target,
        overlap_threshold,
        grace_ns,
        timeout_ns,
        clock,
        summary_interval_ns=SUMMARY_INTERVAL_NS,
    ):
        if local == remote:
            raise ValueError(f"the local and remote engines are both {local!r}")
        if not 0 < overlap_threshold <= 1:
            raise ValueError(
                f"the overlap threshold must be in (0, 1], not {overlap_threshold}"
            )
        self.local = local
        self.remote = remote
        self.target = target.casefold()
        self.threshold = overlap_threshold
        self.grace_ns = grace_ns
        self.timeout_ns = timeout_ns
        self.clock = clock
        self.summary_interval_ns = summary_interval_ns
        self.mode = Mode.LOCAL_ONLY
        self._tracks = OrderedDict()
        self._disconnected = set()
        self._current = None
        self._timeout_at_ns = None
        # The newest input a mode switch was made on; older inputs no longer
        # switch the mode
        self._switched_on = None
        self._summary = None

    # Modes

    def _switch(self, id, mode, why):
        """Goes to `mode` because of the input `id` (None for an event that
        names no input), unless the mode was already switched on a newer
        input."""
        if mode == self.mode:
            return
        if id is not None and _older(id, self._switched_on):
            return
        if id is not None:
            self._switched_on = id
        where = "" if id is None else f"input {id.seq}: "
        logger.info("%s%s -> %s: %s", where, self.mode, mode, why)
        self._stats().switches[(str(self.mode), str(mode))] += 1
        self.mode = mode

    def _unreachable(self, id, why):
        self._switch(id, Mode.LOCAL_ONLY, why)

    def _late(self, track):
        """Whether the remote answer came after the grace period that
        started with the local answer."""
        local = track.answers.get(self.local)
        remote = track.answers.get(self.remote)
        return (
            local is not None
            and remote is not None
            and remote.at_ns > local.at_ns + self.grace_ns
        )

    def _on_remote_answer(self, id, track):
        if self._late(track):
            lateness = (
                track.answers[self.remote].at_ns
                - track.answers[self.local].at_ns
                - self.grace_ns
            )
            self._unreachable(
                id, f"remote answered {lateness / 1e6:.0f} ms after the grace period"
            )
        elif self.mode == Mode.LOCAL_ONLY:
            self._switch(id, Mode.LOCAL_CHECKED, "remote answered in time")

    def _check(self, id, track):
        """Checks the answers of the input `id` against each other, once both
        are in and the remote one was in time."""
        if (
            track.checked
            or self.local not in track.answers
            or self.remote not in track.answers
            or self._late(track)
        ):
            return
        track.checked = True
        result = check(
            track.answers[self.local].data,
            track.answers[self.remote].data,
            self.target,
            self.threshold,
        )
        self._stats().checks[result.verdict] += 1
        logger.debug(
            "input %d check: %s (%s) | local: %s | remote: %s",
            id.seq,
            result.verdict,
            result.why,
            track.status(self.local),
            track.status(self.remote),
        )
        if result.verdict == Verdict.DISAGREE and self.mode == Mode.LOCAL_CHECKED:
            self._switch(id, Mode.REMOTE_ONLY, f"local and remote disagree, {result.why}")
        elif result.verdict == Verdict.AGREE and self.mode == Mode.REMOTE_ONLY:
            self._switch(id, Mode.LOCAL_CHECKED, f"local and remote agree, {result.why}")

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
            if engine == self.remote:
                self._unreachable(None, "remote disconnected")
            return
        if kind == uriel.EventKind.LOST:
            logger.warning("%d events of %s were lost", event.lost, engine)
            self._fail_all(engine, LOST)
            return
        if engine in self._disconnected:
            logger.info("%s is reachable again", engine)
            self._disconnected.discard(engine)
        if kind == uriel.EventKind.RESULT:
            track = self._track(event.id)
            dropped = event.outcome == uriel.Outcome.DROP and not event.result
            track.failed.pop(engine, None)
            track.answers[engine] = Result(
                None if dropped else event.result, event.payload_type, self.clock()
            )
            if engine == self.remote or self.remote in track.answers:
                self._on_remote_answer(event.id, track)
            self._check(event.id, track)
            self._forget_if_done(event.id, track)
        elif kind in _FAILED:
            track = self._track(event.id)
            track.fail(engine, _FAILED[kind])
            if engine == self.remote and kind == uriel.EventKind.TIMED_OUT:
                self._unreachable(event.id, "remote timed out")
            self._forget_if_done(event.id, track)

    def _track(self, id):
        track = self._tracks.get(id)
        if track is None:
            track = self._tracks[id] = Track()
            while len(self._tracks) > MAX_TRACKED:
                oldest = next(iter(self._tracks))
                if oldest == self._current:
                    self._tracks.move_to_end(oldest)
                else:
                    del self._tracks[oldest]
        return track

    def _fail_all(self, engine, why):
        for id, track in list(self._tracks.items()):
            track.fail(engine, why)
            self._forget_if_done(id, track)

    def _forget_if_done(self, id, track):
        """Forgets an input once it is decided and both engines are done
        with it."""
        if track.decided and track.settled() and id != self._current:
            self._tracks.pop(id, None)

    def _name(self, engine):
        return "local" if engine == self.local else "remote"

    # Inputs

    def begin(self, id):
        self._current = id
        self._timeout_at_ns = self.clock() + self.timeout_ns
        track = self._track(id)
        for engine in self._disconnected:
            track.fail(engine, DISCONNECTED)

    def decide(self, id):
        """What to do with the input `id` now: a Wait or a Report."""
        track = self._track(id)
        if self.mode == Mode.REMOTE_ONLY:
            return self._decide_remote(id, track)
        return self._decide_local(id, track)

    def _decide_local(self, id, track):
        if self.local in track.answers:
            return self._report(id, track, self.local, "local answered")
        if self.local in track.failed:
            why = track.failed[self.local]
            if self.remote in track.answers:
                return self._report(id, track, self.remote, f"local {why}")
            if self.remote in track.failed:
                return self._report(id, track, None, "neither engine answered")
        if self.clock() >= self._timeout_at_ns:
            if self.remote in track.answers:
                return self._report(id, track, self.remote, "local timed out")
            return self._report(id, track, None, "timed out")
        return Wait(self._timeout_at_ns)

    def _decide_remote(self, id, track):
        local = track.answers.get(self.local)
        if self.remote in track.answers:
            if not self._late(track):
                return self._report(id, track, self.remote, "remote answered")
            # Late remote answers switch to local-only as they arrive, unless
            # the mode was switched on a newer input; this one is still late
            return self._report(id, track, self.local, "remote was late")
        if self.remote in track.failed:
            why = track.failed[self.remote]
            # Fall back to local when the remote has no answer: it skipped the
            # input (it was busy, but is still reachable), timed out or
            # disconnected (which already switched to local-only unless on a
            # newer input), or its events were lost
            if local is not None:
                return self._report(id, track, self.local, f"remote {why}")
            if self.local in track.failed or self.clock() >= self._timeout_at_ns:
                return self._report(id, track, None, f"remote {why}")
            return Wait(self._timeout_at_ns)
        now = self.clock()
        if local is not None:
            deadline = local.at_ns + self.grace_ns
            if now >= min(deadline, self._timeout_at_ns):
                self._unreachable(id, "no remote answer within the grace period")
                return self._report(id, track, self.local, "remote missed the grace period")
            return Wait(min(deadline, self._timeout_at_ns))
        if now >= self._timeout_at_ns:
            self._unreachable(id, "no remote answer before the timeout")
            return self._report(id, track, None, "timed out")
        return Wait(self._timeout_at_ns)

    def _report(self, id, track, source, reason):
        """The Report of the input `id`, with the answer of the engine
        `source` (None for no result)."""
        answer = None if source is None else track.answers[source]
        name = None if source is None or answer.data is None else self._name(source)
        mode = self.mode
        track.decided = True
        local = track.answers.get(self.local)
        remote = track.answers.get(self.remote)
        timing = ""
        if local is not None and remote is not None:
            timing = f" {(remote.at_ns - local.at_ns) / 1e6:+.0f} ms"
        chose = f"chose: {name or 'none'} ({reason})"
        logger.info(
            "input %-7d %-13s %s| %s | remote%s: %s",
            id.seq,
            mode,
            chose.ljust(DECISION_WIDTH),
            f"local: {track.status(self.local)}".ljust(LOCAL_WIDTH),
            timing,
            track.status(self.remote),
        )
        self._count(name, mode)
        if name is None:
            return Report()
        return Report(answer, source)

    def _stats(self):
        if self._summary is None:
            self._summary = Summary(self.clock())
        return self._summary

    def _count(self, chosen, mode):
        now = self.clock()
        self._stats()
        self._summary.inputs += 1
        self._summary.chosen[chosen or "none"] += 1
        self._summary.modes[mode] += 1
        if now - self._summary.started_ns >= self.summary_interval_ns:
            for line in self._summary.lines(now):
                logger.info("%s", line)
            self._summary = Summary(now)

    def end(self, id):
        """Finishes the input `id`. It is kept until both engines are done
        with it, so that it can still be checked."""
        if self._current == id:
            self._current = None
        track = self._tracks.get(id)
        if track is not None:
            self._forget_if_done(id, track)

    @property
    def tracked(self):
        """How many inputs' events are kept."""
        return len(self._tracks)


def _older(id, other):
    """Whether the input `id` came before `other` from the same producer
    session."""
    return (
        other is not None
        and id.producer == other.producer
        and id.session == other.session
        and id.seq < other.seq
    )
