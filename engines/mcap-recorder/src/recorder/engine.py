"""The engine loop: a uriel thread that never waits on the disk, and a writer
thread that records.

The uriel thread copies each input out of shared memory and drops it at once,
so the recorder returns its producer's token right away and never slows the
flow. Copies go to the writer thread through a bounded queue; if the writer
falls behind and the queue is full, items are dropped (with a warning)
rather than block. An H.264 frame dropped this way smears the recorded video
until the next keyframe."""

import collections
import logging
import queue
import threading
import time

import uriel

from recorder.files import VehicleFiles
from recorder.recorder import Item, Recorder

logger = logging.getLogger("recorder")

# Most bytes of copied inputs waiting for the writer: about 20 720p RawFrames.
QUEUE_BYTES = 64 << 20
POLL_INTERVAL_S = 0.1
WRITER_POLL_S = 0.5
FLUSH_INTERVAL_S = 2.0
RESUBSCRIBE_DELAY_S = 2.0
DROP_WARNING_INTERVAL_S = 10.0
# The recorder sends nothing on, so it needs no room for outputs.
MAX_OUTPUT_SIZE = 1024

_STOP = object()


class ByteQueue:
    """A FIFO bounded by the bytes of the items in it (an Item's data, or a
    bytes item), so large raw frames can't pile up. Never blocks on put."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._bytes = 0
        self._items: collections.deque = collections.deque()
        self._ready = threading.Condition()

    @staticmethod
    def _size(item) -> int:
        if isinstance(item, (bytes, bytearray)):
            return len(item)
        return len(getattr(item, "data", b""))

    def put_nowait(self, item) -> None:
        size = self._size(item)
        with self._ready:
            if size and self._bytes + size > self.max_bytes:
                raise queue.Full
            self._bytes += size
            self._items.append(item)
            self._ready.notify()

    put = put_nowait

    def get(self, timeout: float | None = None):
        with self._ready:
            if not self._ready.wait_for(lambda: self._items, timeout):
                raise queue.Empty
            item = self._items.popleft()
            self._bytes -= self._size(item)
            return item


def enqueue(q, item) -> bool:
    """Puts `item` on `q` without waiting; False if `q` is full."""
    try:
        q.put_nowait(item)
        return True
    except queue.Full:
        return False


def run(
    name: str,
    recorder: Recorder,
    files: VehicleFiles,
    subscriptions: list[str],
    *,
    stop: threading.Event,
    on_ready=None,
) -> None:
    """Serves the engine `name`, recording its inputs and the results of the
    engines `subscriptions`, until `stop` is set; then closes every file."""
    q = ByteQueue(QUEUE_BYTES)
    writer = threading.Thread(
        target=_write, args=(recorder, files, q), name="writer", daemon=True
    )
    writer.start()
    try:
        _serve(name, subscriptions, q, stop, on_ready)
    finally:
        q.put(_STOP)
        writer.join()
        files.close_all()


def _serve(name, subscriptions, q, stop, on_ready):
    dropped = 0
    last_drop_warning = 0.0
    with uriel.Engine(name, max_output_size=MAX_OUTPUT_SIZE) as engine:
        pending = list(subscriptions)
        next_subscribe = 0.0
        ready = False
        while not stop.is_set():
            if pending and time.monotonic() >= next_subscribe:
                pending = _subscribe(engine, pending)
                next_subscribe = time.monotonic() + RESUBSCRIBE_DELAY_S
            if not ready:
                logger.info(
                    "engine %s ready; recording results of %s",
                    name,
                    ", ".join(subscriptions),
                )
                ready = True
                if on_ready is not None:
                    on_ready()
            engine.wait(POLL_INTERVAL_S)
            items = []
            while (event := engine.poll_event()) is not None:
                if event.kind == uriel.EventKind.LOST:
                    logger.warning(
                        "%d events of %s were lost", event.lost, event.engine
                    )
                elif event.kind == uriel.EventKind.RESULT and event.result:
                    items.append(
                        Item(
                            "event",
                            event.id,
                            event.payload_type,
                            bytes(event.result),
                            event.engine,
                            time.time_ns(),
                        )
                    )
            input = engine.poll()
            if input is not None:
                items.append(
                    Item(
                        "input",
                        input.id,
                        input.payload_type,
                        bytes(input.data),
                        "",
                        time.time_ns(),
                    )
                )
                engine.drop()
            for item in items:
                if not enqueue(q, item):
                    dropped += 1
            now = time.monotonic()
            if dropped and now - last_drop_warning >= DROP_WARNING_INTERVAL_S:
                logger.warning("writer behind: dropped %d inputs/results", dropped)
                dropped = 0
                last_drop_warning = now


def _subscribe(engine, names):
    """Subscribes to each of `names`, returning those that failed."""
    failed = []
    for name in names:
        try:
            engine.subscribe(name)
            logger.info("subscribed to %s", name)
        except Exception as err:
            logger.warning("could not subscribe to %s (%s); retrying", name, err)
            failed.append(name)
    return failed


def _write(recorder: Recorder, files: VehicleFiles, q: ByteQueue) -> None:
    last_flush = time.monotonic()
    while True:
        try:
            item = q.get(timeout=WRITER_POLL_S)
        except queue.Empty:
            item = None
        if item is _STOP:
            return
        if item is not None:
            recorder.handle(item)
        try:
            for vehicle in files.close_idle():
                recorder.forget(vehicle)
            if time.monotonic() - last_flush >= FLUSH_INTERVAL_S:
                files.flush()
                last_flush = time.monotonic()
        except Exception:
            logger.exception("could not flush or close recordings")
