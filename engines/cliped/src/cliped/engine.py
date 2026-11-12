"""The engine: each RawFrame is cut into tiles, every tile is scored against
the prompt, the tiles worth sending are queued by score, and the input is
replaced with the best queued tile that is not in flight, or dropped if there
is none. Tiles leave the queue only once the verifier has them: its RESULT
event for a tile (see send_queue.py).

The output is the tile as an RGB RawFrame at its native resolution, with the
id, timestamp and pose of the frame it was cut from; an encoder after this
engine (e.g. the thumbnail plugin's WebP encoder) compresses it for the link.
The result is a DetectionResult of the tiles this input picked, which may not
be the tile it carries: one per picked tile, of class `prompt`, with its score
s and its box in the frame.

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
from cliped.send_queue import SendQueue, Tile
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.result import result_pb2

logger = logging.getLogger("cliped")

RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"
DETECTION_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/DetectionResult"
# The verifier's result: a JSON object {"match": bool}
GENERIC_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/GenericResult"
RGB_CHANNELS = 3
# Room in an output for a tile's fields besides its pixels, and for the
# result: its ids, timestamp and pose, and a detection per picked tile.
OUTPUT_HEADROOM = 64 << 10
# How long the engine waits for an input or event before it checks whether
# to stop.
POLL_INTERVAL_S = 0.1
# Fields a tile keeps of the frame it was cut from.
_FRAME_FIELDS = ("timestamp", "gimbal_info", "position_info")


def qualify(qualifier, name):
    """`name` qualified with a daemon's IP, as eagled names its services, or
    as is if there is no qualifier."""
    return f"{qualifier}.{name}" if qualifier else name


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
    has them."""

    def __init__(self, encoder, selector, queue, *, prompt, verifier, tile_size, stride, log_scores=False,
                 tile_log=None):
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
        """The tile to send in place of `input` (a send_queue.Tile) or None,
        and the result: the serialized DetectionResult of the tiles it
        picked. Raises FrameError if `input` is not an RGB RawFrame."""
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

        result = result_pb2.DetectionResult()
        for i, must in zip(picked, must_send):
            box = boxes[i]
            score = float(s[i])
            self.queue.push(
                Tile(score=score, payload=tile_frame(frame, rgb, box), frame_id=frame.id, box=box, index=int(i),
                     embedding=v[i], must_send=bool(must))
            )
            x0, y0, x1, y1 = box
            result.detections.add(
                class_name=self.prompt,
                score=score,
                bbox=result_pb2.BoundingBox(x_min=x0 / width, y_min=y0 / height, x_max=x1 / width, y_max=y1 / height),
            )
        self.picked += len(picked)

        tile = self.queue.next(input.id, now)
        if tile is not None and self.tile_log is not None:
            self.tile_log.output(tile.frame_id, tile.index, read_frame(tile.payload)[1])
        if self.log_scores or len(picked):
            logger.info(
                "frame %d: %d tiles in %.0f ms, best s %.3f, thr %.3f, picked %d; queue %d (%d in flight)%s",
                frame.id,
                len(boxes),
                (time.perf_counter() - started) * 1e3,
                float(s.max()),
                self.selector.thr,
                len(picked),
                len(self.queue),
                self.queue.in_flight,
                f"; sending the tile of frame {tile.frame_id} (s {tile.score:.3f})" if tile is not None else "",
            )
        return tile, result.SerializeToString()


def run(name, cliped, *, max_output_size, stop, on_ready=None):
    """Serves the engine `name`, subscribed to the verifier, until the
    threading.Event `stop` is set."""
    with uriel.Engine(name, result_type=DETECTION_RESULT_TYPE, max_output_size=max_output_size) as engine:
        engine.subscribe(cliped.verifier)
        logger.info("engine %s ready; searching for %r, verified by %s", name, cliped.prompt, cliped.verifier)
        if on_ready is not None:
            on_ready()
        while not stop.is_set():
            engine.wait(POLL_INTERVAL_S)
            while (event := engine.poll_event()) is not None:
                try:
                    cliped.on_event(event)
                except Exception:
                    logger.exception("failed to handle a %s event", event.kind)
            input = engine.poll()
            if input is None:
                continue
            if input.payload_type.replace(".", "/") != RAW_FRAME_TYPE:
                logger.warning("input %d is a %r, not a RawFrame; dropping it", input.id.seq, input.payload_type)
                engine.drop()
                continue
            try:
                tile, result = cliped.on_input(input)
            except FrameError as err:
                logger.warning("input %d: %s; dropping it", input.id.seq, err)
                engine.drop()
                continue
            except Exception:
                logger.exception("input %d: failed to process it; dropping it", input.id.seq)
                engine.drop()
                continue
            if tile is None:
                engine.drop(result=result)
                continue
            if len(tile.payload) + len(result) > max_output_size:
                logger.warning("the tile of frame %d is %d bytes, over max_output_size; dropping it",
                               tile.frame_id, len(tile.payload))
                cliped.queue.discard(tile)  # it can never be sent
                engine.drop(result=result)
                continue
            engine.loan()[: len(tile.payload)] = tile.payload
            engine.replace(len(tile.payload), output_type=RAW_FRAME_TYPE, result=result)
