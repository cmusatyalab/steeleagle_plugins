"""The engine: each RawFrame is cut into tiles, every tile is scored against
the prompt, and the tiles worth sending are queued by score until the
verifier has them: its RESULT event for a tile (see send_queue.py).

Each input is replaced with the best tile cut from it, or dropped if it has
none; the result is a ClassificationResult of that tile: class the prompt
(the text, or the image's file name), score s. That tile is queued too, with
the input's other picked tiles, and the engine's reintegration producer
(Reintegration) sends the queue's waiting tiles, best first, as the flow has
room for them. So a tile not yet confirmed (lost, timed out, or found while
the link was down) still reaches the verifier when no frames come in, or when
every frame's token is taken.

A tile is an RGB RawFrame at its native resolution, with the id, timestamp
and pose of the frame it was cut from; an encoder after this engine and after
its producer (e.g. the thumbnail plugin's WebP encoder) compresses it for the
link.

With learning on, the verifier's RESULT for a tile also teaches the selector
(clip.Selector.learn): tiles like one it said yes to are sent, and tiles like
one it said no to, again and again, are not; when that changes, every tile
still queued is rescored, and the ones that no longer pass are dropped.
"""
import json
import logging
import time

import numpy as np
import uriel
from google.protobuf.message import DecodeError

from cliped.clip import ADDED_GOOD, CONFIRMED, cut_tiles, tile_boxes
from cliped.send_queue import Tile
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.result import result_pb2

logger = logging.getLogger("cliped")

RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"
CLASSIFICATION_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/ClassificationResult"
# The verifier's result: a JSON object {"match": bool}
GENERIC_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/GenericResult"
RGB_CHANNELS = 3
# Room in an output for a tile's fields besides its pixels, and for the
# result: its ids, timestamp and pose, and its classification.
OUTPUT_HEADROOM = 64 << 10
# How long the engine waits for an input or an event of the verifier before it
# reads its producer's events, sends a queued tile, and checks whether to
# stop. It bounds how long the producer takes to see its last tile admitted.
POLL_INTERVAL_S = 0.02
# The producer that sends queued tiles, as cliped.plugin.toml advertises it;
# the engine publishes on {engine name}.{this}.
REINTEGRATION = "reintegration"
# Fields a tile keeps of the frame it was cut from.
_FRAME_FIELDS = ("timestamp", "gimbal_info", "position_info")


def qualify(qualifier, name):
    """`name` qualified with a daemon's IP, as eagled names its services, or
    as is if there is no qualifier."""
    return f"{qualifier}.{name}" if qualifier else name


def producer_name(engine, producer):
    """The name the engine `engine` publishes its producer `producer` on, as
    eagled names it."""
    return f"{engine}.{producer}"


def max_output_size(tile_size):
    """The largest tile this engine sends, with its result."""
    return tile_size * tile_size * RGB_CHANNELS + OUTPUT_HEADROOM


class FrameError(ValueError):
    pass


def read_verdict(event):
    """The verifier's answer in its RESULT `event`, True or False; or None
    if the result is not a GenericResult holding {"match": true|false}."""
    if event.payload_type.replace(".", "/") != GENERIC_RESULT_TYPE:
        return None
    try:
        answer = json.loads(result_pb2.GenericResult.FromString(event.result).payload)
    except (DecodeError, ValueError) as err:
        logger.warning("unreadable verifier result: %s", err)
        return None
    if not isinstance(answer, dict) or not isinstance(answer.get("match"), bool):
        logger.warning("verifier result %r has no boolean 'match'", answer)
        return None
    return answer["match"]


def read_frame(data):
    """The serialized RGB RawFrame `data`, and its pixels as an (h, w, 3)
    array. Raises FrameError for anything else."""
    try:
        frame = frame_pb2.RawFrame.FromString(data)
    except DecodeError as err:
        raise FrameError(f"not a RawFrame: {err}") from None
    width, height = frame.h_res, frame.v_res
    channels = frame.channels or RGB_CHANNELS
    if channels != RGB_CHANNELS or width == 0 or len(frame.data) != width * height * RGB_CHANNELS:
        raise FrameError(f"{len(frame.data)} bytes, not a {width}x{height} RGB frame")
    return frame, np.frombuffer(frame.data, dtype=np.uint8).reshape(height, width, RGB_CHANNELS)


def tile_frame(frame, rgb, box):
    """The serialized RawFrame of the tile `box` of `frame`."""
    x0, y0, x1, y1 = box
    crop = np.ascontiguousarray(rgb[y0:y1, x0:x1])
    out = frame_pb2.RawFrame(
        data=crop.tobytes(),
        h_res=x1 - x0,
        v_res=y1 - y0,
        channels=RGB_CHANNELS,
        id=frame.id,
        camera_id=frame.camera_id,
    )
    for name in _FRAME_FIELDS:
        if frame.HasField(name):
            getattr(out, name).CopyFrom(getattr(frame, name))
    return out.SerializeToString()


class Cliped:
    """Turns inputs into the tiles to send. `encoder` embeds tiles, `selector`
    picks them (clip.Selector), and `queue` holds them until the verifier
    has them; Reintegration sends the ones waiting."""

    def __init__(self, encoder, selector, queue, *, prompt, verifier, tile_size, stride, log_scores=False,
                 tile_log=None, sent_log=None):
        self.encoder = encoder
        self.selector = selector
        self.queue = queue
        self.prompt = prompt
        self.verifier = verifier
        self.tile_size = tile_size
        self.stride = stride
        self.log_scores = log_scores
        # A files.TileLog, which gets each input and each tile sent, or None
        self.tile_log = tile_log
        # A sent_log.SentLog each tile queued is logged to, or None
        self.sent_log = sent_log
        self._boxes = {}  # (width, height) -> tile boxes
        self.frames = 0
        self.picked = 0

    def on_event(self, event):
        if event.engine != self.verifier:
            return
        tile = self.queue.on_event(event)
        if tile is not None and self.selector.learning:
            self.learn(tile, event)

    def learn(self, tile, event):
        """Learns from the verifier's result about `tile`, then rescores the
        queue if that changed what the selector knows."""
        match = read_verdict(event)
        if match is None or tile.embedding is None:
            return
        outcome = self.selector.learn(tile.embedding, match)
        message = "verifier: %s for the tile of frame %d: %s (%d good, %d background tiles)"
        args = ["yes" if match else "no", tile.frame_id, outcome, len(self.selector.good),
                len(self.selector.backgrounds)]
        if outcome in (ADDED_GOOD, CONFIRMED):
            dropped = self.queue.rescore(lambda t: self.selector.passes(t.embedding))
            message += "; rescored the queue, dropping %d (%d left)"
            args += [len(dropped), len(self.queue)]
        logger.info(message, *args)

    def boxes(self, width, height):
        key = (width, height)
        if key not in self._boxes:
            self._boxes[key] = tile_boxes(width, height, self.tile_size, self.stride)
            logger.info("cutting %dx%d frames into %d tiles", width, height, len(self._boxes[key]))
        return self._boxes[key]

    def on_input(self, input, now=None):
        """The tile to send in place of `input` (a send_queue.Tile), the best
        tile cut from it, or None if it has none; and the result: the
        serialized ClassificationResult of that tile, or b"". Every tile it
        picked is queued, and the one sent is in flight as `input`. Raises
        FrameError if `input` is not an RGB RawFrame."""
        now = time.monotonic() if now is None else now
        frame, rgb = read_frame(bytes(input.data))
        if self.tile_log is not None:
            self.tile_log.input(frame.id, rgb)
        height, width = rgb.shape[:2]
        boxes = self.boxes(width, height)
        started = time.perf_counter()
        v = self.encoder.encode_tiles(cut_tiles(rgb, boxes, self.encoder.size))
        picked, s = self.selector.select(v)
        must_send = self.selector.score(v[picked])[1] == 1 if len(picked) else []
        self.frames += 1

        tiles = [
            Tile(score=float(s[i]), payload=tile_frame(frame, rgb, boxes[i]), frame_id=frame.id, box=boxes[i],
                 index=int(i), embedding=v[i], must_send=bool(must))
            for i, must in zip(picked, must_send)
        ]
        for tile in tiles:
            self.queue.push(tile)
            if self.sent_log is not None:
                self.sent_log.write(frame, tile, width, height, self.prompt)
        self.picked += len(tiles)

        tile = max(tiles, key=lambda t: t.priority, default=None)
        result = b""
        if tile is not None:
            self.queue.sent(tile, input.id, now)
            result = result_pb2.ClassificationResult(score=tile.score, **{"class": self.prompt}).SerializeToString()
            if self.tile_log is not None:
                self.tile_log.output(tile.frame_id, tile.index, read_frame(tile.payload)[1])
        if self.log_scores or tiles:
            logger.info(
                "frame %d: %d tiles in %.0f ms, best s %.3f, thr %.3f, picked %d; queue %d (%d in flight)%s",
                frame.id,
                len(boxes),
                (time.perf_counter() - started) * 1e3,
                float(s.max()),
                self.selector.thr,
                len(tiles),
                len(self.queue),
                self.queue.in_flight,
                f"; sending its tile {tile.index} (s {tile.score:.3f})" if tile is not None else "",
            )
        return tile, result


class Reintegration:
    """The engine's reintegration producer: sends the tiles waiting in
    `queue`, best first, whether or not inputs come in.

    Uriel's client keeps only a producer's newest input until the flow has
    room for it, and an input it replaces there never enters the flow, with no
    event. So one tile at a time waits to be admitted: the next goes once an
    event (`on_event`) shows the last one entered the flow, unless a better
    tile turns up, which replaces it, and it waits again. A tile admitted is
    in flight like any other until the verifier's events settle it
    (send_queue.py)."""

    def __init__(self, queue, *, tile_log=None):
        self.queue = queue
        self.tile_log = tile_log
        # The tile published last and its input id, until admitted
        self._pending = None
        self.published = 0

    def on_event(self, event):
        """Takes an event about the producer's inputs, at any engine, or of
        the verifier: any event about the tile waiting means it was
        admitted."""
        if self._pending is not None and event.id == self._pending[1]:
            self._pending = None

    def step(self, now, publish):
        """Publishes the best waiting tile with `publish(payload)`, which
        returns its input id, unless the tile published last still waits to
        be admitted and is no worse. Returns the tile published, or None."""
        best = self.queue.best(now)
        if self._pending is not None:
            tile, id = self._pending
            if tile not in self.queue or not tile.in_flight or tile.carriers[-1] != id:
                self._pending = None  # confirmed, dropped, or put back to wait
            elif best is None or best.priority <= tile.priority:
                return None
            else:
                # The better tile replaces it at the client, so it never leaves
                self.queue.unsend(tile, id)
                self._pending = None
                best = self.queue.best(now)
        if best is None:
            return None
        id = publish(best.payload)
        self.queue.sent(best, id, now)
        self._pending = (best, id)
        self.published += 1
        if self.tile_log is not None:
            self.tile_log.output(best.frame_id, best.index, read_frame(best.payload)[1])
        logger.info("reintegration: sending the tile of frame %d (s %.3f); queue %d (%d in flight)",
                    best.frame_id, best.score, len(self.queue), self.queue.in_flight)
        return best


def run(name, cliped, *, max_output_size, stop, on_ready=None):
    """Serves the engine `name`, subscribed to the verifier, and its
    reintegration producer, until the threading.Event `stop` is set."""
    producer = producer_name(name, REINTEGRATION)
    reintegration = Reintegration(cliped.queue, tile_log=cliped.tile_log)
    with (
        uriel.Engine(name, result_type=CLASSIFICATION_RESULT_TYPE, max_output_size=max_output_size) as engine,
        uriel.Producer(producer, input_type=RAW_FRAME_TYPE, max_input_size=max_output_size) as tiles,
        uriel.Subscription.producer(producer) as admissions,
    ):
        engine.subscribe(cliped.verifier)

        def publish(payload):
            return uriel.InputId(producer, tiles.session, tiles.send(payload))

        logger.info("engine %s ready, with producer %s; searching for %r, verified by %s",
                    name, producer, cliped.prompt, cliped.verifier)
        if on_ready is not None:
            on_ready()
        while not stop.is_set():
            engine.wait(POLL_INTERVAL_S)
            while (event := engine.poll_event()) is not None:
                try:
                    cliped.on_event(event)
                    reintegration.on_event(event)
                except Exception:
                    logger.exception("failed to handle a %s event", event.kind)
            while (event := admissions.poll()) is not None:
                reintegration.on_event(event)
            if (input := engine.poll()) is not None:
                _process(engine, cliped, input, max_output_size)
            try:
                reintegration.step(time.monotonic(), publish)
            except Exception:
                logger.exception("reintegration failed to send a tile")


def _process(engine, cliped, input, max_output_size):
    """Reports the outcome of `input`: replaced with its tile, or dropped."""
    if input.payload_type.replace(".", "/") != RAW_FRAME_TYPE:
        logger.warning("input %d is a %r, not a RawFrame; dropping it", input.id.seq, input.payload_type)
        engine.drop()
        return
    try:
        tile, result = cliped.on_input(input)
    except FrameError as err:
        logger.warning("input %d: %s; dropping it", input.id.seq, err)
        engine.drop()
        return
    except Exception:
        logger.exception("input %d: failed to process it; dropping it", input.id.seq)
        engine.drop()
        return
    if tile is None:
        engine.drop()
        return
    if len(tile.payload) + len(result) > max_output_size:
        logger.warning("the tile of frame %d is %d bytes, over max_output_size; dropping it",
                       tile.frame_id, len(tile.payload))
        cliped.queue.discard(tile)  # it can never be sent
        engine.drop(result=result)
        return
    engine.loan()[: len(tile.payload)] = tile.payload
    engine.replace(len(tile.payload), output_type=RAW_FRAME_TYPE, result=result)
