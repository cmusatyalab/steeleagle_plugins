"""The reachback engine, on the vehicle: keyframe selection for SwiftMap, and
reach back. It stores frames with where the camera was and where it looked,
forwards keyframes (early_discard.KeyframeFilter) and drops the rest.

It subscribes to the swiftmap engine on the remote daemon. When that engine
asks for the frames covering the weak targets of a plan, each frame that comes
in is only stored, and its place in the flow carries the next frame sent back
(REACHBACK_FRAME_TYPE), then a REACHBACK_DONE_TYPE once all are sent. When it
says to hold (the server closed a batch in NFN mode), frames are only stored
and none is forwarded, besides those sent back, until it says to release. Put
it right after the JPEG encoder."""

import logging
import math
import sqlite3
import time

import numpy as np
import uriel
from google.protobuf.message import DecodeError

from steeleagle_protocol.v1.messages.frame import frame_pb2
from swiftmap.engine import (
    ENCODED_FRAME_TYPE, REACHBACK_DONE_TYPE, REACHBACK_FRAME_TYPE, REQUEST_TYPE, FrameError,
    decode_request, frame_gps,
)

logger = logging.getLogger("swiftmap.reachback")

POLL_INTERVAL_S = 0.1
KEEP_FRAMES = 6000
STORE_INTERVAL_S = 0.2
HALF_FOV_DEG = 25.0
MAX_DISTANCE_M = 50.0
M_PER_DEG = 111_320.0
FORWARD, DROP = "forward", "drop"


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

    def take(self, rowid):
        """The frame `rowid`, marked sent."""
        rowid = int(rowid)
        with self._db:
            self._db.execute("UPDATE frames SET sent = 1 WHERE rowid = ?", (rowid,))
            return self._db.execute("SELECT data FROM frames WHERE rowid = ?", (rowid,)).fetchone()[0]


class Reachback:
    """Stores a frame with a camera pose every `interval` seconds per camera,
    and every keyframe `keyframes` keeps, as sent. Sends the frames asked for
    in place of the frames coming in, before any more keyframes, and forwards
    no keyframes while holding."""

    def __init__(self, store, keyframes, interval=STORE_INTERVAL_S):
        self.store = store
        self.keyframes = keyframes
        self.interval = interval
        self.holding = False
        self.queue = []
        self._last_stored = {}

    def on_request(self, id, payload):
        """Holds or releases keyframes, or queues the frames covering the
        targets asked about, round-robin up to the count asked for; ValueError
        if `payload` is not a request."""
        targets, count, hold = decode_request(payload)
        if hold is not None:
            self.holding = hold
            logger.info("%s keyframes", "holding" if hold else "releasing")
            return
        rowids, numbers, poses = self.store.unsent(id.producer, id.session)
        chosen, taken = [], set()
        while len(chosen) < count:
            before = len(chosen)
            for target in targets[:count - len(chosen)]:
                i = covering(poses, numbers, target, taken)
                if i is not None:
                    taken.add(int(numbers[i]))
                    chosen.append(rowids[i])
            if len(chosen) == before:
                break
        self.queue = ([(REACHBACK_FRAME_TYPE, self.store.take(r)) for r in chosen]
                      + [(REACHBACK_DONE_TYPE, b"done")])
        logger.info("sending %d frames back for %d targets", len(chosen), len(targets))

    def on_input(self, input, now):
        """FORWARD, DROP, or (output type, payload) to send in place of `input`.
        Raises FrameError if it is not a frame the keyframe filter reads."""
        if self.queue or self.holding:
            self._store(input, now, sent=False)
            return self.queue.pop(0) if self.queue else DROP
        keep = self.keyframes.keep(input)
        self._store(input, now, sent=keep)
        return FORWARD if keep else DROP

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
                if event.kind != uriel.EventKind.DERIVED or event.payload_type != REQUEST_TYPE:
                    continue
                try:
                    reachback.on_request(event.id, event.result)
                except ValueError as err:
                    logger.warning("bad request from %s: %s", remote, err)
                except Exception:
                    logger.exception("failed to answer %s", remote)
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
