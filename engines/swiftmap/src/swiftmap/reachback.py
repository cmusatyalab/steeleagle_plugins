"""The reachback engine: stores every frame and forwards it. It subscribes to
the swiftmap engine on the remote daemon and answers its requests for lost
frames by transferring them to that daemon as files (reachback-<n>.frame),
outside the flow. Put it right after the JPEG encoder, whose frame numbers
the requests name."""

import logging
import queue
import sqlite3
import threading

import grpc
import uriel
from google.protobuf.message import DecodeError

from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.services.daemon import transfer_pb2, transfer_pb2_grpc
from swiftmap.engine import ENCODED_FRAME_TYPE, REQUEST_TYPE, decode_request, frame_filename

logger = logging.getLogger("swiftmap.reachback")

POLL_INTERVAL_S = 0.1
MAX_OUTPUT_SIZE = 4096  # it only forwards, but Uriel needs a bound
CHUNK_SIZE = 1 << 20  # as eagle sends files
ROUTE_HEADER = "x-route-key"
TRANSFER_TIMEOUT_S = 120.0
# swiftmap never asks for older frames: it gives up after gap_timeout or 300 held
KEEP_FRAMES = 1000


class FrameStore:
    """The newest `keep` EncodedFrames, by producer, session and frame number,
    in SQLite."""

    def __init__(self, path=":memory:", keep=KEEP_FRAMES):
        self.keep = keep
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(
            """
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            CREATE TABLE IF NOT EXISTS frames (
                producer TEXT NOT NULL, session TEXT NOT NULL,
                number INTEGER NOT NULL, data BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS frames_by_number ON frames (producer, session, number);
            """
        )

    def close(self):
        self._db.close()

    def add(self, producer, session, number, data):
        with self._db:
            row = self._db.execute(
                "INSERT INTO frames VALUES (?, ?, ?, ?)", (producer, str(session), number, data)
            ).lastrowid
            self._db.execute("DELETE FROM frames WHERE rowid <= ?", (row - self.keep,))

    def frames(self, producer, session, first, last):
        """(number, data) of the frames numbered first to last, in order; the
        last stored of each number."""
        rows = self._db.execute(
            "SELECT number, data FROM frames WHERE producer = ? AND session = ? "
            "AND number BETWEEN ? AND ? ORDER BY number, rowid",
            (producer, str(session), first, last),
        )
        return sorted(dict(rows).items())


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
                    # One ack per chunk; the stream ends once the file is saved
                    for _ in stub.TransferFile(chunks(name, data), timeout=TRANSFER_TIMEOUT_S,
                                               metadata=[(ROUTE_HEADER, self.route)]):
                        pass
                    logger.info("transferred %s (%d bytes) to %s", name, len(data), self.route)
                except grpc.RpcError as err:
                    logger.warning("failed to transfer %s to %s: %s: %s",
                                   name, self.route, err.code().name, err.details())


class Reachback:
    """Stores frames, and sends back those asked for with `transfer(name, data)`."""

    def __init__(self, store, transfer):
        self.store = store
        self.transfer = transfer

    def on_input(self, input):
        if input.payload_type.replace(".", "/") != ENCODED_FRAME_TYPE:
            logger.warning("input %d: not an EncodedFrame; not stored", input.id.seq)
            return
        data = bytes(input.data)
        try:
            frame = frame_pb2.EncodedFrame.FromString(data)
        except DecodeError as err:
            logger.warning("input %d: bad EncodedFrame (%s); not stored", input.id.seq, err)
            return
        self.store.add(input.id.producer, input.id.session, frame.id, data)

    def on_request(self, id, payload):
        """Sends back the frames asked for; ValueError if `payload` is no request."""
        first, last = decode_request(payload)
        frames = self.store.frames(id.producer, id.session, first, last)
        logger.info("frames %d to %d asked for; sending the %d stored", first, last, len(frames))
        for number, data in frames:
            self.transfer(frame_filename(number), data)


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
                reachback.on_input(input)
            except Exception:
                logger.exception("input %d: failed to store it", input.id.seq)
            engine.forward()
