"""The reachback engine: stores frames, with where the camera was and where it
looked, and forwards every frame. It subscribes to the swiftmap engine on the
remote daemon, and for each weak target that engine asks about, transfers the
stored frame that sees the target closest to the centre of its view to that
daemon as a file (reachback-<n>.frame), outside the flow. Put it after the
JPEG encoder and before the keyframe engine, so it sees every frame."""

import logging
import math
import queue
import sqlite3
import threading
import time

import grpc
import numpy as np
import uriel
from google.protobuf.message import DecodeError

from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.services.daemon import transfer_pb2, transfer_pb2_grpc
from swiftmap.engine import ENCODED_FRAME_TYPE, REQUEST_TYPE, decode_request, frame_filename, frame_gps

logger = logging.getLogger("swiftmap.reachback")

POLL_INTERVAL_S = 0.1
MAX_OUTPUT_SIZE = 4096
CHUNK_SIZE = 1 << 20
ROUTE_HEADER = "x-route-key"
TRANSFER_TIMEOUT_S = 120.0
KEEP_FRAMES = 6000
STORE_INTERVAL_S = 0.2
HALF_FOV_DEG = 25.0
MAX_DISTANCE_M = 50.0
M_PER_DEG = 111_320.0


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

    def add(self, producer, session, number, pose, data):
        with self._db:
            row = self._db.execute(
                "INSERT INTO frames (producer, session, number, lat, lon, alt, yaw, pitch, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (producer, str(session), number, *pose, data),
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


def chunks(name, data, chunk_size=CHUNK_SIZE):
    total = max(1, -(-len(data) // chunk_size))
    for i in range(total):
        yield transfer_pb2.TransferFileRequest(
            chunk=data[i * chunk_size:(i + 1) * chunk_size], number=i + 1, total=total, filename=name,
        )


class Transfer:
    """Sends files to the daemon `route` with the TransferService of the local
    daemon at `socket_path`, one at a time on its own thread."""

    def __init__(self, socket_path, route):
        self.socket_path = socket_path
        self.route = route
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="transfer", daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._queue.put(None)
        self._thread.join(TRANSFER_TIMEOUT_S)

    def send(self, name, data):
        self._queue.put((name, data))

    def _run(self):
        with grpc.insecure_channel(f"unix://{self.socket_path}") as channel:
            stub = transfer_pb2_grpc.TransferServiceStub(channel)
            while (item := self._queue.get()) is not None:
                name, data = item
                try:
                    for _ in stub.TransferFile(chunks(name, data), timeout=TRANSFER_TIMEOUT_S,
                                               metadata=[(ROUTE_HEADER, self.route)]):
                        pass
                    logger.info("transferred %s (%d bytes) to %s", name, len(data), self.route)
                except grpc.RpcError as err:
                    logger.warning("failed to transfer %s to %s: %s: %s",
                                   name, self.route, err.code().name, err.details())


class Reachback:
    """Stores a frame with a camera pose every `interval` seconds per camera,
    and sends the frames covering the targets asked about with
    `transfer(name, data)`."""

    def __init__(self, store, transfer, interval=STORE_INTERVAL_S):
        self.store = store
        self.transfer = transfer
        self.interval = interval
        self._last_stored = {}

    def on_input(self, input, now):
        stream = (input.id.producer, input.id.session)
        if input.payload_type.replace(".", "/") != ENCODED_FRAME_TYPE or (
                now - self._last_stored.get(stream, float("-inf")) < self.interval):
            return
        data = bytes(input.data)
        try:
            frame = frame_pb2.EncodedFrame.FromString(data)
        except DecodeError as err:
            logger.warning("input %d: bad EncodedFrame (%s); not stored", input.id.seq, err)
            return
        pose = frame_pose(frame)
        if pose is not None:
            self.store.add(*stream, frame.id, pose, data)
            self._last_stored[stream] = now

    def on_request(self, id, payload):
        """Sends the frame covering each target asked about; ValueError if
        `payload` is not a request."""
        found, exclude = decode_request(payload)
        rowids, numbers, poses = self.store.unsent(id.producer, id.session)
        sent = []
        for target in found:
            i = covering(poses, numbers, target, exclude)
            if i is not None:
                self.transfer(frame_filename(numbers[i]), self.store.take(rowids[i]))
                exclude.add(int(numbers[i]))
                sent.append(int(numbers[i]))
        logger.info("sending frames %s for %d targets", sent, len(found))


def run(name, reachback, remote, *, stop, on_ready=None):
    """Serves the engine `name`, answering the engine `remote`, until `stop` is set."""
    with uriel.Engine(name, max_output_size=MAX_OUTPUT_SIZE) as engine:
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
                reachback.on_input(input, time.monotonic())
            except Exception:
                logger.exception("input %d: failed to store it", input.id.seq)
            engine.forward()
