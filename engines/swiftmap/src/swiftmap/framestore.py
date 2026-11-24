"""The framestore engine, on the vehicle, after the encoder. It is a dead end:
each camera frame is stored with the camera's pose and dropped. A frame the
keyframe filter keeps (onboard.KeyframeFilter) is published on its keyframes
producer. The swiftmap engine's NFN status says when to hold keyframes, and
asks for frames covering the weak targets of a plan: one stored frame per
target, published on its sendback producer one at a time, then an empty end
marker. Give each producer one token, so Uriel paces both by the link."""

import logging
import math
import sqlite3
import time
from collections import deque

import numpy as np
import uriel
from google.protobuf.message import DecodeError

from steeleagle_protocol.v1.messages.frame import frame_pb2
from swiftmap.protocol import (
    ENCODED_FRAME_TYPE, KEYFRAMES, POLL_INTERVAL_S, SENDBACK, STATUS_TYPE, FrameError,
    decode_status, frame_gps, producer_name, same_type,
)

logger = logging.getLogger("swiftmap.framestore")

KEEP_FRAMES = 6000
STORE_INTERVAL_S = 0.2
HALF_FOV_DEG = 25.0
MAX_DISTANCE_M = 50.0
M_PER_DEG = 111_320.0
END = frame_pb2.EncodedFrame().SerializeToString()
TURN_ENDED = (uriel.EventKind.RESULT, uriel.EventKind.SKIPPED, uriel.EventKind.TIMED_OUT,
              uriel.EventKind.DISCONNECTED, uriel.EventKind.LOST)


def frame_pose(frame):
    """(lat, lon, alt, yaw, pitch) of the camera, or None without GPS or gimbal pose."""
    gps = frame_gps(frame)
    if gps is None or not (frame.HasField("gimbal_info") and frame.gimbal_info.HasField("pose_neu")):
        return None
    pose = frame.gimbal_info.pose_neu
    return (*gps, pose.yaw, pose.pitch)


def to_enu(lla, origin):
    """(N, 3) east, north, up metres of (N, 3) lat, lon, alt about `origin`."""
    return np.column_stack([
        (lla[:, 1] - origin[1]) * M_PER_DEG * math.cos(math.radians(origin[0])),
        (lla[:, 0] - origin[0]) * M_PER_DEG,
        lla[:, 2] - origin[2],
    ])


def covering(poses, numbers, target, exclude):
    """Index of the frame seeing `target` nearest its view's centre, within
    HALF_FOV_DEG and MAX_DISTANCE_M, skipping `exclude`d numbers; or None."""
    cams = to_enu(poses[:, :3], target)
    dist = np.linalg.norm(cams, axis=1)
    yaw, pitch = np.radians(poses[:, 3]), np.radians(poses[:, 4])
    look = np.column_stack([np.sin(yaw) * np.cos(pitch), np.cos(yaw) * np.cos(pitch), np.sin(pitch)])
    centred = -(look * cams).sum(1) / np.maximum(dist, 1e-9)
    ok = ((centred >= math.cos(math.radians(HALF_FOV_DEG))) & (dist <= MAX_DISTANCE_M)
          & ~np.isin(numbers, list(exclude)))
    return int(np.flatnonzero(ok)[np.argmax(centred[ok])]) if ok.any() else None


class FrameStore:
    """The newest `keep` EncodedFrames with their poses and whether sent, in SQLite."""

    def __init__(self, path=":memory:", keep=KEEP_FRAMES):
        self.keep = keep
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript("""
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            CREATE TABLE IF NOT EXISTS frames (
                number INTEGER NOT NULL, lat REAL, lon REAL, alt REAL, yaw REAL, pitch REAL,
                sent INTEGER NOT NULL DEFAULT 0, data BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS frames_by_sent ON frames (sent);
        """)

    def close(self):
        self._db.close()

    def add(self, number, pose, data, sent=False):
        with self._db:
            row = self._db.execute(
                "INSERT INTO frames (number, lat, lon, alt, yaw, pitch, sent, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (number, *pose, int(sent), data),
            ).lastrowid
            self._db.execute("DELETE FROM frames WHERE rowid <= ?", (row - self.keep,))

    def unsent(self):
        """(rowids, numbers, (N, 5) poses) of the frames not sent yet."""
        rows = self._db.execute(
            "SELECT rowid, number, lat, lon, alt, yaw, pitch FROM frames WHERE sent = 0").fetchall()
        rows = np.array(rows, dtype=float).reshape(-1, 7)
        return rows[:, 0].astype(int), rows[:, 1].astype(int), rows[:, 2:]

    def take(self, rowid):
        """The frame `rowid`, marked sent, or None if it is no longer kept."""
        with self._db:
            row = self._db.execute("SELECT data FROM frames WHERE rowid = ?", (int(rowid),)).fetchone()
            self._db.execute("UPDATE frames SET sent = 1 WHERE rowid = ?", (int(rowid),))
        return None if row is None else row[0]


class FrameStoreEngine:
    """Stores a posed frame every `interval` s and every keyframe; publishes
    keyframes unless holding or sending back; for a request, publishes one
    covering frame per target, one at a time, then END."""

    def __init__(self, store, keyframes, interval=STORE_INTERVAL_S):
        self.store = store
        self.keyframes = keyframes
        self.interval = interval
        self.holding = False
        self.request = None
        self.queue = deque()
        self.out = None
        self._last_stored = float("-inf")

    @property
    def sending_back(self):
        return bool(self.queue) or self.out is not None

    def on_status(self, payload):
        """Holds keyframes or not, and starts a request it has not started:
        the frame covering each target best, for up to its count of targets."""
        hold, request = decode_status(payload)
        if hold != self.holding:
            self.holding = hold
            logger.info("%s keyframes", "holding" if hold else "releasing")
        if request is None or request == self.request:
            return
        self.request = request
        targets, count = request
        rowids, numbers, poses = self.store.unsent()
        chosen, taken = [], set()
        for target in targets[:count]:
            if (i := covering(poses, numbers, target, taken)) is not None:
                taken.add(int(numbers[i]))
                chosen.append(rowids[i])
        frames = [data for data in map(self.store.take, chosen) if data is not None]
        self.queue.extend(frames + [END])
        logger.info("sending %d frames back for %d targets", len(frames), len(targets))

    def on_sendback_event(self, event):
        """Frees the sendback producer once the frame it carries has had its turn."""
        if event.kind in TURN_ENDED and (event.id == self.out or event.id is None):
            self.out = None

    def on_input(self, input, now, publish_keyframe):
        """Stores the camera frame `input`, and publishes it as a keyframe if it
        is one and keyframes are not held."""
        if not (self.holding or self.sending_back) and self.keyframes.keep(input):
            self._store(input, now, sent=True)
            publish_keyframe(bytes(input.data))
        else:
            self._store(input, now, sent=False)

    def send_next(self, publish_sendback):
        """Publishes the next frame sent back once the last one's turn ended."""
        if self.out is None and self.queue:
            self.out = publish_sendback(self.queue.popleft())

    def _store(self, input, now, sent):
        if not (sent or now - self._last_stored >= self.interval):
            return
        if not same_type(input.payload_type, ENCODED_FRAME_TYPE):
            return
        try:
            frame = frame_pb2.EncodedFrame.FromString(bytes(input.data))
        except DecodeError:
            return
        if (pose := frame_pose(frame)) is not None:
            self.store.add(frame.id, pose, bytes(input.data), sent)
            self._last_stored = now


def run(name, framestore, remote, *, max_output_size, stop, on_ready=None):
    """Serves the engine `name`, following the status of the engine `remote`,
    with its keyframes and sendback producers, until `stop` is set."""
    keyframes_name, sendback_name = producer_name(name, KEYFRAMES), producer_name(name, SENDBACK)
    with (
        uriel.Engine(name, max_output_size=max_output_size) as engine,
        uriel.Producer(keyframes_name, input_type=ENCODED_FRAME_TYPE, max_input_size=max_output_size) as keyframes,
        uriel.Producer(sendback_name, input_type=ENCODED_FRAME_TYPE, max_input_size=max_output_size) as sendback,
        uriel.Subscription.producer(sendback_name) as sendback_events,
    ):
        engine.subscribe(remote)
        logger.info("engine %s ready, with producers %s and %s; following %s",
                    name, keyframes_name, sendback_name, remote)
        if on_ready is not None:
            on_ready()

        def publish_sendback(data):
            return uriel.InputId(sendback_name, sendback.session, sendback.send(data))

        while not stop.is_set():
            engine.wait(POLL_INTERVAL_S)
            while (event := engine.poll_event()) is not None:
                if event.kind == uriel.EventKind.DERIVED and event.payload_type == STATUS_TYPE:
                    try:
                        framestore.on_status(event.result)
                    except ValueError as err:
                        logger.warning("bad NFN status from %s: %s", remote, err)
            while (event := sendback_events.poll()) is not None:
                framestore.on_sendback_event(event)
            if (input := engine.poll()) is not None:
                try:
                    framestore.on_input(input, time.monotonic(), keyframes.send)
                except FrameError as err:
                    logger.warning("input %d: %s", input.id.seq, err)
                except Exception:
                    logger.exception("input %d: failed to handle it", input.id.seq)
                engine.drop()
            framestore.send_next(publish_sendback)
