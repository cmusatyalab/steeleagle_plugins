"""The engine: each RawFrame is cut into tiles, every tile is scored against
the prompt, and the tiles worth sending are queued by score until the
verifier has them: its RESULT event for a tile (see send_queue.py).

Each input is replaced with the best tile cut from it, or dropped if it has
none; the result is a ClassificationResult of that tile: class the prompt
(the text, or the image's file name), score s. That forwarded tile is not
tracked: in a flow where the engine is a dead end (no edge out of it), it
goes nowhere. Every tile picked is queued, and the engine's reintegration
producer (Reintegration) delivers the queue: one tile at a time, best first,
each until the verifier's RESULT for it, sent again when its turn in the flow
ends without one. Give the producer one token: the flow then paces it.

A tile is an RGB RawFrame at its native resolution, with the id, timestamp
and pose of the frame it was cut from; an encoder after this engine and after
its producer (e.g. the thumbnail plugin's WebP encoder) compresses it for the
link.

With learning on, the verifier's RESULT for a tile also teaches the selector
(clip.Selector.learn): tiles like one it said yes to are sent, and tiles like
one it said no to, again and again, are not; when that changes, every tile
still queued is rescored, and the ones that no longer pass are dropped.
With `visual`, a SimilarFilter, two things change. A no is kept from being
learned (guarded) only if its tile is visually a tile the verifier said yes
to, ORB keypoints matching, rather than CLIP-alike (visual_guard): CLIP
embeddings of aerial tiles are too alike to tell. And each answer is also
learned for every tile picked in the last `propagate_s` seconds that is
visually the answered tile (propagation): a target or distractor seen in
frame after frame is confirmed by one answer rather than by the verifier
seeing it twice.

The engine follows the verifier's prompt, as box_rerank does: it subscribes to
the verifier and takes each prompt heartbeat (a DERIVED event of type
prompt.PROMPT_TYPE). With no prompt or image of its own, it drops every frame
until the first heartbeat, sending the verifier a primer tile (the middle of
a frame, never learned from) at most every PRIME_INTERVAL_S while nothing is
queued: the verifier's heartbeat is about an input, so it starts only once
the verifier has one. Then, and whenever the verifier's prompt changes,
it searches for that text, learning over: the tiles waiting are dropped, and
answers about tiles sent for an earlier prompt are not learned from. With
ignore_new, it keeps its own prompt and doesn't subscribe.
"""
import collections
import json
import logging
import time

import numpy as np
import uriel
from google.protobuf.message import DecodeError

from cliped.clip import CONFIRMED, CONFIRMED_GOOD, DROPPED_GOOD, cut_tiles, tile_boxes
from cliped.prompt import heartbeat_prompt, is_heartbeat
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
# Without a prompt, the least time between two primer tiles sent so that the
# verifier has an input, which its first prompt heartbeat needs
PRIME_INTERVAL_S = 5.0
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


# read_verdict's answer for a frame the verifier was unsure about: forwarded,
# but not to be learned from
UNSURE = "unsure"


def read_verdict(event):
    """The verifier's answer in its RESULT `event`, True or False, or UNSURE
    for {"match": null, "unsure": true}; or None if the result is not a
    GenericResult holding one of them."""
    if event.payload_type.replace(".", "/") != GENERIC_RESULT_TYPE:
        return None
    try:
        answer = json.loads(result_pb2.GenericResult.FromString(event.result).payload)
    except (DecodeError, ValueError) as err:
        logger.warning("unreadable verifier result: %s", err)
        return None
    if isinstance(answer, dict) and answer.get("unsure") is True:
        return UNSURE
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
                 tile_log=None, sent_log=None, scored_log=None, similar=None, has_prompt=True, retarget=None,
                 ignore_new=False, visual=None, visual_guard=False, propagate_s=0.0):
        self.encoder = encoder
        self.selector = selector
        self.queue = queue
        # The prompt: its text, or an image's file name ("" for none yet);
        # whether there is one at all (without one, every frame is dropped);
        # and retarget(text), the selector for the text prompt `text`, for
        # the verifier's prompt (on_prompt), or None if it can't switch
        self.prompt = prompt
        self.has_prompt = has_prompt
        self.retarget = retarget
        # Whether to keep this prompt whatever the verifier's is
        self.ignore_new = ignore_new
        # How many prompts it has switched to: an answer about a tile of an
        # earlier one isn't learned from
        self.generation = 0
        self.verifier = verifier
        self.tile_size = tile_size
        self.stride = stride
        self.log_scores = log_scores
        # A files.TileLog, which gets each input and each tile sent, or None
        self.tile_log = tile_log
        # A sent_log.SentLog each tile queued is logged to, or None
        self.sent_log = sent_log
        # A sent_log.ScoredLog each frame scored is logged to, or None
        self.scored_log = scored_log
        # A similar.SimilarFilter that tells the tiles like one sent before,
        # which go behind the new ones, or None (defer_similar off)
        self.similar = similar
        # A similar.SimilarFilter whose ORB keypoints and matching the visual
        # guard and propagation use, or None for neither
        self.visual = visual
        self.visual_guard = visual_guard and visual is not None
        self.propagate_s = propagate_s if visual is not None else 0.0
        # The tiles picked in the last propagate_s: (time.monotonic(), tile)
        self._recent = collections.deque()
        # The time of the latest input, which propagate_s is counted back from
        self._last_now = None
        # The keypoints of the tiles the verifier said yes to, newest last
        self._good_features = collections.deque(maxlen=max(1, getattr(selector, "max_learned", 32)))
        self._boxes = {}  # (width, height) -> tile boxes
        self.frames = 0
        self.picked = 0
        # Frames dropped for want of a prompt, and when the next primer tile
        # may be sent (time.monotonic())
        self.unprompted = 0
        self._next_primer = None

    def on_event(self, event):
        """Takes an event about a reintegration input, at any engine."""
        tile = self.queue.on_event(event, self.verifier)
        if tile is not None and self.selector.learning:
            self.learn(tile, event)

    def learn(self, tile, event):
        """Learns from the verifier's result about `tile`, then rescores the
        queue if that changed what the selector knows."""
        match = read_verdict(event)
        if match is None or tile.embedding is None or not self.has_prompt:
            return
        if tile.generation != self.generation:
            logger.info("verifier: an answer about the tile of frame %d, sent for an earlier prompt; not learned",
                        tile.frame_id)
            return
        tile.answered = True
        if match == UNSURE:
            logger.info("verifier: unsure about the tile of frame %d; not learned", tile.frame_id)
            return
        outcome = self._learn_one(tile, match)
        changed = outcome in (CONFIRMED_GOOD, CONFIRMED, DROPPED_GOOD)
        propagated = []
        for other, n in self._like(tile):
            also = self._learn_one(other, match)
            changed |= also in (CONFIRMED_GOOD, CONFIRMED, DROPPED_GOOD)
            propagated.append(f"{other.frame_id} ({n} keypoints): {also}")
        message = "verifier: %s for the tile of frame %d: %s (%d good, %d confirmed; %d background tiles, %d confirmed)"
        args = ["yes" if match else "no", tile.frame_id, outcome, len(self.selector.good),
                len(self.selector.confirmed_good), len(self.selector.backgrounds),
                sum(n >= self.selector.confirmations for n in self.selector.noes)]
        if propagated:
            message += "; also learned for the tiles like it of frames %s"
            args.append(", ".join(propagated))
        if changed:
            dropped = self.queue.rescore(lambda t: self.selector.passes(t.embedding))
            message += "; rescored the queue, dropping %d (%d left)"
            args += [len(dropped), len(self.queue)]
        logger.info(message, *args)

    def _learn_one(self, tile, match):
        """Learns the answer `match` for `tile`: with the visual guard, a no
        is guarded only if the tile's keypoints match a tile said yes to."""
        guarded = None
        if not match and self.visual_guard and tile.features is not None:
            guarded = any(self.visual.matches(tile.features, g) >= self.visual.min_matches
                          for g in self._good_features)
        outcome = self.selector.learn(tile.embedding, match, guarded=guarded)
        if match and tile.features is not None:
            self._good_features.append(tile.features)
        return outcome

    def _like(self, tile, now=None):
        """The tiles picked in the last propagate_s, for the current prompt and
        not answered, that are visually `tile`, with how many keypoints
        match. Each is learned once per answer it is like."""
        if not self.propagate_s or tile.features is None:
            return []
        now = (self._last_now if self._last_now is not None else time.monotonic()) if now is None else now
        while self._recent and now - self._recent[0][0] > self.propagate_s:
            self._recent.popleft()
        like = []
        for _, other in self._recent:
            if (other is tile or other.answered or other.generation != self.generation
                    or other.features is None or other.embedding is None):
                continue
            n = self.visual.same(tile.features, other.features, tile.embedding, other.embedding)
            if n >= self.visual.min_matches:
                like.append((other, n))
        return like

    def on_prompt_event(self, event):
        """Takes an event of the verifier: switches to the prompt its
        heartbeat holds (on_prompt). Returns whether the event was a
        heartbeat. With ignore_new, a heartbeat changes nothing."""
        if not is_heartbeat(event, self.verifier):
            return False
        if self.ignore_new:
            return True
        prompt = heartbeat_prompt(event)
        if prompt is not None:
            # The description is what CLIP searches for, else the class
            self.on_prompt(prompt[0])
        return True

    def on_prompt(self, text):
        """Switches to the text prompt `text`, the verifier's, if it isn't the
        current one, and starts learning over: what the verifier said about
        the old prompt says nothing about the new one. The tiles waiting are
        dropped; answers about the ones in flight are not learned from.
        Returns whether it switched."""
        text = " ".join((text or "").split())
        if not text or (self.has_prompt and text == self.prompt):
            return False
        if self.retarget is None:
            logger.warning("the verifier's prompt is %r, but this engine can't switch prompts", text)
            self.prompt, self.has_prompt = text, True  # warn once; keeps searching for its own
            return False
        old = self.prompt if self.has_prompt else None
        self.selector = self.retarget(text)
        self.prompt, self.has_prompt = text, True
        self.generation += 1
        dropped = self.queue.rescore(lambda tile: (False, tile.score, False))
        if self.similar is not None:
            self.similar.forget()
        self._recent.clear()
        self._good_features.clear()
        logger.info("prompt: now %r (was %s), the verifier's; learning cleared, %d waiting tiles dropped%s",
                    text, "none" if old is None else repr(old), len(dropped),
                    f"; {self.unprompted} frames dropped while it had none" if old is None and self.unprompted else "")
        return True

    def _prime(self, frame, rgb, now):
        """Without a prompt, queues the middle tile of `frame` (`rgb`) for the
        verifier, unless a tile waits or one was primed in the last
        PRIME_INTERVAL_S: the verifier's prompt heartbeat starts with its
        first input. It has no embedding, so its answer is never learned."""
        if len(self.queue) or (self._next_primer is not None and now < self._next_primer):
            return
        boxes = self.boxes(rgb.shape[1], rgb.shape[0])
        cx, cy = rgb.shape[1] / 2, rgb.shape[0] / 2
        i = min(range(len(boxes)), key=lambda j: abs((boxes[j][0] + boxes[j][2]) / 2 - cx)
                + abs((boxes[j][1] + boxes[j][3]) / 2 - cy))
        self.queue.push(Tile(score=0.0, payload=tile_frame(frame, rgb, boxes[i]), frame_id=frame.id, box=boxes[i],
                             index=i, generation=self.generation))
        self._next_primer = now + PRIME_INTERVAL_S
        logger.info("no prompt yet: sending the verifier the middle tile of frame %d, for its prompt heartbeat",
                    frame.id)

    def boxes(self, width, height):
        key = (width, height)
        if key not in self._boxes:
            self._boxes[key] = tile_boxes(width, height, self.tile_size, self.stride)
            logger.info("cutting %dx%d frames into %d tiles", width, height, len(self._boxes[key]))
        return self._boxes[key]

    def on_input(self, input, now=None):
        """The tile to forward in place of `input` (a send_queue.Tile), the
        best tile cut from it, or None if it has none; and the result: the
        serialized ClassificationResult of that tile, or b"". Every tile it
        picked is queued for reintegration, the one forwarded included.
        Raises FrameError if `input` is not an RGB RawFrame."""
        now = time.monotonic() if now is None else now
        self._last_now = now
        frame, rgb = read_frame(bytes(input.data))
        if not self.has_prompt:
            # Nothing to search for until the verifier's prompt heartbeat
            self.unprompted += 1
            self._prime(frame, rgb, now)
            return None, b""
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
                 index=int(i), embedding=v[i], must_send=bool(must), generation=self.generation)
            for i, must in zip(picked, must_send)
        ]
        for tile in tiles:
            x0, y0, x1, y1 = tile.box
            if self.visual is not None:
                tile.features = self.visual.features(np.ascontiguousarray(rgb[y0:y1, x0:x1]))
                if self.propagate_s:
                    self._recent.append((now, tile))
            if self.similar is not None:
                tile.similar = not self.similar.is_new(rgb[y0:y1, x0:x1], tile.embedding, frame.id,
                                                       feats=tile.features)
            self.queue.push(tile)
            if self.sent_log is not None:
                self.sent_log.write(frame, tile, width, height, self.prompt)
        self.picked += len(tiles)
        if self.scored_log is not None:
            self.scored_log.write(frame, len(boxes), float(s.max()), self.selector.thr, len(tiles),
                                  (time.perf_counter() - started) * 1e3)

        tile = max(tiles, key=lambda t: t.priority, default=None)
        result = b""
        if tile is not None:
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
                (f"; sending its tile {tile.index} (s {tile.score:.3f}"
                 f"{', like one sent before' if tile.similar else ''})") if tile is not None else "",
            )
        return tile, result


class Reintegration:
    """The engine's reintegration producer: delivers the tiles waiting in
    `queue`, best first, whether or not inputs come in, one at a time: the
    next goes only once the last one's turn in the flow ended (the queue
    settles it on the flow's events; see send_queue.py). Its producer has one
    token, so the flow paces it, and with only one input published at a time
    the client never replaces one before it enters the flow."""

    def __init__(self, queue, *, tile_log=None):
        self.queue = queue
        self.tile_log = tile_log
        # The tile published last and its input id, until its turn ends
        self._current = None
        self.published = 0

    def step(self, now, publish):
        """Publishes the best waiting tile with `publish(payload)`, which
        returns its input id, unless the tile published last is still in the
        flow. Returns the tile published, or None."""
        if self._current is not None:
            tile, id = self._current
            if tile in self.queue and tile.in_flight and tile.carriers[-1] == id:
                return None
            self._current = None  # confirmed, dropped, or put back to wait
        best = self.queue.best()
        if best is None:
            return None
        id = publish(best.payload)
        self.queue.sent(best, id, now)
        self._current = (best, id)
        self.published += 1
        if self.tile_log is not None:
            self.tile_log.output(best.frame_id, best.index, read_frame(best.payload)[1])
        logger.info("reintegration: sending the tile of frame %d (s %.3f)%s; queue %d",
                    best.frame_id, best.score, f", try {len(best.carriers)}" if len(best.carriers) > 1 else "",
                    len(self.queue))
        return best


def run(name, cliped, *, max_output_size, stop, on_ready=None):
    """Serves the engine `name` and its reintegration producer, following
    the events about the producer's inputs at every engine, until the
    threading.Event `stop` is set."""
    producer = producer_name(name, REINTEGRATION)
    reintegration = Reintegration(cliped.queue, tile_log=cliped.tile_log)
    with (
        uriel.Engine(name, result_type=CLASSIFICATION_RESULT_TYPE, max_output_size=max_output_size) as engine,
        uriel.Producer(producer, input_type=RAW_FRAME_TYPE, max_input_size=max_output_size) as tiles,
        uriel.Subscription.producer(producer) as events,
    ):

        # For the verifier's prompt heartbeat
        if not cliped.ignore_new:
            engine.subscribe(cliped.verifier)

        def publish(payload):
            return uriel.InputId(producer, tiles.session, tiles.send(payload))

        logger.info("engine %s ready, with producer %s; searching for %s, verified by %s",
                    name, producer, repr(cliped.prompt) if cliped.has_prompt else "the verifier's prompt",
                    cliped.verifier)
        if on_ready is not None:
            on_ready()
        while not stop.is_set():
            engine.wait(POLL_INTERVAL_S)
            while (event := engine.poll_event()) is not None:
                try:
                    cliped.on_prompt_event(event)
                except Exception:
                    logger.exception("failed to handle a %s event", event.kind)
            while (event := events.poll()) is not None:
                try:
                    cliped.on_event(event)
                except Exception:
                    logger.exception("failed to handle a %s event", event.kind)
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
