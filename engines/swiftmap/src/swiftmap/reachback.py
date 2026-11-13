"""The reachback engine, on the vehicle: keyframe selection for SwiftMap, and
reach back. It stores frames with where the camera was and where it looked,
forwards keyframes (early_discard.KeyframeFilter) and drops the rest.

It subscribes to the swiftmap engine on the remote daemon, which tells it the
NFN status every second: whether to hold (the server closed a batch in NFN
mode), and the current request for frames covering the weak targets of a
plan. While holding, frames are only stored and none is forwarded, besides
those sent back. For a request it has not seen, each frame that comes in is
only stored, and its place in the flow carries the next frame sent back
(REACHBACK_FRAME_TYPE), then a REACHBACK_DONE_TYPE once all are in, each
stamped with the request.

The same subscription says what became of each input at the swiftmap engine,
and so whether what was sent back arrived. A frame that did not is sent
again, up to MAX_TRIES times, and the end until it arrives. Nothing is sent
back while no status has come for LINK_S, so the link being down does not use
up the tries. Put it right after the JPEG encoder."""

import logging
import math
import sqlite3
import time
from collections import deque

import numpy as np
import uriel
from google.protobuf.message import DecodeError

from steeleagle_protocol.v1.messages.frame import frame_pb2
from swiftmap.engine import (
    ENCODED_FRAME_TYPE, REACHBACK_DONE_TYPE, REACHBACK_FRAME_TYPE, STATUS_TYPE, FrameError,
    decode_status, frame_gps, stamp,
)

logger = logging.getLogger("swiftmap.reachback")

POLL_INTERVAL_S = 0.1
KEEP_FRAMES = 6000
STORE_INTERVAL_S = 0.2
HALF_FOV_DEG = 25.0
MAX_DISTANCE_M = 50.0
M_PER_DEG = 111_320.0
FORWARD, DROP = "forward", "drop"
# Times a frame sent back may fail to arrive (skipped or timed out) before it
# is given up
MAX_TRIES = 3
# Seconds with no status from the swiftmap engine after which the link counts
# as down, and nothing is sent back until one comes
LINK_S = 3.0
# Seconds with no word of what became of something sent back before it is
# sent again; uriel reports every outcome, so this only covers a report lost
REPORT_TIMEOUT_S = 30.0


def frame_pose(frame):
    """(lat, lon, alt, yaw, pitch) of the camera that took the frame, yaw and
    pitch in degrees from north and the horizon; None without a GPS fix or
    gimbal pose."""
    gps = frame_gps(frame)
    if gps is None or not (frame.HasField("gimbal_info") and frame.gimbal_info.HasField("pose_neu")):
        return None
    pose = frame.gimbal_info.pose_neu
    return (*gps, pose.yaw, pose.pitch)


def to_enu(lla, origin):
    """(N, 3) east, north, up metres of the (N, 3) lat, lon, alt about `origin`."""
    return np.column_stack([
        (lla[:, 1] - origin[1]) * M_PER_DEG * math.cos(math.radians(origin[0])),
        (lla[:, 0] - origin[0]) * M_PER_DEG,
        lla[:, 2] - origin[2],
    ])


def covering(poses, numbers, target, exclude):
    """Index of the frame that sees `target` closest to the centre of its view,
    within HALF_FOV_DEG of it and MAX_DISTANCE_M away; None if none does."""
    cams = to_enu(poses[:, :3], target)
    dist = np.linalg.norm(cams, axis=1)
    yaw, pitch = np.radians(poses[:, 3]), np.radians(poses[:, 4])
    look = np.column_stack([np.sin(yaw) * np.cos(pitch), np.cos(yaw) * np.cos(pitch), np.sin(pitch)])
    centred = -(look * cams).sum(1) / np.maximum(dist, 1e-9)
    ok = ((centred >= math.cos(math.radians(HALF_FOV_DEG))) & (dist <= MAX_DISTANCE_M)
          & ~np.isin(numbers, list(exclude)))
    return int(np.flatnonzero(ok)[np.argmax(centred[ok])]) if ok.any() else None


class FrameStore:
    """The newest `keep` EncodedFrames, with their camera poses and whether
    they were sent, in SQLite."""

    def __init__(self, path=":memory:", keep=KEEP_FRAMES):
        self.keep = keep
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(
            """
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            CREATE TABLE IF NOT EXISTS frames (
                producer TEXT NOT NULL, session TEXT NOT NULL, number INTEGER NOT NULL,
                lat REAL, lon REAL, alt REAL, yaw REAL, pitch REAL,
                sent INTEGER NOT NULL DEFAULT 0, data BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS frames_by_stream ON frames (producer, session, sent);
            """
        )

    def close(self):
        self._db.close()

    def add(self, producer, session, number, pose, data, sent=False):
        with self._db:
            row = self._db.execute(
                "INSERT INTO frames (producer, session, number, lat, lon, alt, yaw, pitch, sent, "
                "data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (producer, str(session), number, *pose, int(sent), data),
            ).lastrowid
            self._db.execute("DELETE FROM frames WHERE rowid <= ?", (row - self.keep,))

    def unsent(self, producer, session):
        """(rowids, numbers, (N, 5) poses) of the frames not sent yet."""
        rows = self._db.execute(
            "SELECT rowid, number, lat, lon, alt, yaw, pitch FROM frames "
            "WHERE producer = ? AND session = ? AND sent = 0", (producer, str(session)),
        ).fetchall()
        rows = np.array(rows, dtype=float).reshape(-1, 7)
        return rows[:, 0].astype(int), rows[:, 1].astype(int), rows[:, 2:]

    def read(self, rowid):
        """The frame `rowid`, or None if it is no longer kept."""
        row = self._db.execute("SELECT data FROM frames WHERE rowid = ?", (int(rowid),)).fetchone()
        return None if row is None else row[0]

    def mark_sent(self, rowid):
        with self._db:
            self._db.execute("UPDATE frames SET sent = 1 WHERE rowid = ?", (int(rowid),))


class Outgoing:
    """A frame sent back, or the end of them, and how sending it has gone."""

    def __init__(self, output_type, payload, rowid=None):
        self.output_type = output_type
        self.payload = payload
        self.rowid = rowid
        self.failures = 0
        self.delivered = False


class Reachback:
    """Stores a frame with a camera pose every `interval` seconds per camera,
    and every keyframe `keyframes` keeps, as sent. Follows the swiftmap
    engine's NFN status: forwards no keyframes while holding, and sends the
    frames a request asks for in place of the frames coming in, before any
    more keyframes, until each has arrived or been given up, then their end
    until it has arrived. A frame over `max_size` bytes, once stamped, is not
    sent back."""

    def __init__(self, store, keyframes, interval=STORE_INTERVAL_S, max_size=None):
        self.store = store
        self.keyframes = keyframes
        self.interval = interval
        self.max_size = max_size
        self.holding = False
        # The last request started, so a status repeating it is ignored
        self.request_id = None
        # Frames to send back, in order, and their end, until it arrives
        self.todo = deque()
        self.end = None
        # Input id -> what it carried, for every send for the request; and
        # input id -> when it was sent, for those with no outcome yet
        self._sends = {}
        self._waiting = {}
        self._heard = float("-inf")
        self._picked = self._delivered = 0
        self._last_stored = {}

    @property
    def busy(self):
        """Whether anything of the request is left to send back."""
        return bool(self.todo) or self.end is not None

    def on_event(self, event, now):
        """Handles an event of the swiftmap engine: an NFN status, or what
        became of an input there, which may have carried something sent back.
        Raises ValueError for a status that is not one."""
        kind = event.kind
        if kind == uriel.EventKind.DERIVED:
            if event.payload_type == STATUS_TYPE:
                self.on_status(event.id, event.result, now)
        elif kind == uriel.EventKind.RESULT:
            self._settle(event.id, delivered=True)
        elif kind in (uriel.EventKind.SKIPPED, uriel.EventKind.TIMED_OUT):
            self._settle(event.id, delivered=False)
        elif kind in (uriel.EventKind.DISCONNECTED, uriel.EventKind.LOST):
            # The outcome of what is out may never come: send it again, not
            # counting it as a try, since the link failed, not the frame
            for id in list(self._waiting):
                self._settle(id, delivered=False, counts=False)

    def on_status(self, id, payload, now):
        """Holds keyframes or not, and starts on a request it has not seen,
        picking the frames that cover its targets, round-robin up to its
        count. A status without a request drops what is left of the last.
        Raises ValueError if `payload` is not a status."""
        hold, request = decode_status(payload)
        self._heard = now
        if hold != self.holding:
            self.holding = hold
            logger.info("%s keyframes", "holding" if hold else "releasing")
        if request is None:
            if self.busy:
                logger.info("request %d withdrawn; dropping what is left to send back",
                            self.request_id)
                self._reset()
            return
        if request.id == self.request_id:
            return
        self._reset()
        self.request_id = request.id
        rowids, numbers, poses = self.store.unsent(id.producer, id.session)
        chosen, taken = [], set()
        while len(chosen) < request.count:
            before = len(chosen)
            for target in request.targets[:request.count - len(chosen)]:
                i = covering(poses, numbers, target, taken)
                if i is not None:
                    taken.add(int(numbers[i]))
                    chosen.append(rowids[i])
            if len(chosen) == before:
                break
        for rowid in chosen:
            data = self.store.read(rowid)
            payload = None if data is None else stamp(request.id, len(self.todo), data)
            if payload is None or (self.max_size is not None and len(payload) > self.max_size):
                logger.warning("request %d: frame %d is gone from the store or over "
                               "max_output_size; not sending it back", request.id, rowid)
                continue
            self.todo.append(Outgoing(REACHBACK_FRAME_TYPE, payload, rowid))
        self._picked, self._delivered = len(self.todo), 0
        self.end = Outgoing(REACHBACK_DONE_TYPE, stamp(request.id, len(self.todo)))
        logger.info("request %d: sending %d frames back for %d targets",
                    request.id, len(self.todo), len(request.targets))

    def _reset(self):
        self.todo.clear()
        self.end = None
        self._sends.clear()
        self._waiting.clear()

    def _next(self, now):
        """What to send back in place of the next input: the next frame not
        yet delivered, then the end once nothing is out; None if nothing is
        due, or no status has come for LINK_S."""
        if now - self._heard >= LINK_S:
            return None
        while self.todo:
            out = self.todo.popleft()
            if not out.delivered:
                return out
        if self.end is not None and not self._waiting:
            return self.end
        return None

    def _settle(self, id, delivered, counts=True):
        """Records what became of the input `id` at the swiftmap engine. If
        it carried something sent back, that has arrived, or else, unless it
        already has, goes again: the end until it arrives, a frame until it
        has failed MAX_TRIES times, if the failure `counts`."""
        out = self._sends.get(id)
        if out is None:
            return
        waited = self._waiting.pop(id, None) is not None
        if delivered:
            if out.delivered:
                return
            out.delivered = True
            if out is self.end:
                self.end = None
                logger.info("request %d: done, %d of %d frames sent back",
                            self.request_id, self._delivered, self._picked)
            else:
                self._delivered += 1
                self.store.mark_sent(out.rowid)
            return
        # A late failure of an earlier send, or the end, which goes again once
        # nothing else is out
        if not waited or out.delivered or out is self.end:
            return
        if counts:
            out.failures += 1
        if out.failures >= MAX_TRIES:
            logger.warning("request %d: giving up on a frame after %d tries",
                           self.request_id, out.failures)
            return
        self.todo.append(out)

    def expire(self, now):
        """Sends again what has had no word for REPORT_TIMEOUT_S."""
        for id, sent in list(self._waiting.items()):
            if now - sent >= REPORT_TIMEOUT_S:
                self._settle(id, delivered=False, counts=False)

    def on_input(self, input, now):
        """FORWARD, DROP, or (output type, payload) to send in place of `input`.
        Raises FrameError if it is not a frame the keyframe filter reads."""
        out = self._next(now)
        if out is None and not self.holding:
            keep = self.keyframes.keep(input)
            self._store(input, now, sent=keep)
            return FORWARD if keep else DROP
        if out is not None:
            self._sends[input.id] = out
            self._waiting[input.id] = now
        self._store(input, now, sent=False)
        return DROP if out is None else (out.output_type, out.payload)

    def _store(self, input, now, sent):
        stream = (input.id.producer, input.id.session)
        due = now - self._last_stored.get(stream, float("-inf")) >= self.interval
        if not (sent or due) or input.payload_type.replace(".", "/") != ENCODED_FRAME_TYPE:
            return
        data = bytes(input.data)
        try:
            frame = frame_pb2.EncodedFrame.FromString(data)
        except DecodeError:
            return
        pose = frame_pose(frame)
        if pose is not None:
            self.store.add(*stream, frame.id, pose, data, sent)
            self._last_stored[stream] = now


def run(name, reachback, remote, *, max_output_size, stop, on_ready=None):
    """Serves the engine `name`, answering the engine `remote`, until `stop` is set."""
    with uriel.Engine(name, max_output_size=max_output_size) as engine:
        engine.subscribe(remote)
        logger.info("engine %s ready; answering %s", name, remote)
        if on_ready is not None:
            on_ready()
        while not stop.is_set():
            engine.wait(POLL_INTERVAL_S)
            while (event := engine.poll_event()) is not None:
                try:
                    reachback.on_event(event, time.monotonic())
                except ValueError as err:
                    logger.warning("bad NFN status from %s: %s", remote, err)
                except Exception:
                    logger.exception("failed to handle an event of %s", remote)
            reachback.expire(time.monotonic())
            input = engine.poll()
            if input is None:
                continue
            try:
                action = reachback.on_input(input, time.monotonic())
            except FrameError as err:
                logger.warning("input %d: %s; dropping it", input.id.seq, err)
                action = DROP
            except Exception:
                logger.exception("input %d: failed to handle it; dropping it", input.id.seq)
                action = DROP
            if action == FORWARD:
                engine.forward()
            elif action == DROP:
                engine.drop()
            else:
                output_type, payload = action
                engine.loan()[:len(payload)] = payload
                engine.replace(len(payload), output_type=output_type)
