"""The engine loop: each frame is held until the detector's result for it is
in, then replaced with its crop and the crop's ClassificationResult, or
dropped. An engine holds one input at a time, so later frames wait at the
server meanwhile; a replaced input keeps its id.

The crop forwarded is not tracked: in a flow where the engine is a dead end
(no edge out of it), it goes nowhere. Each crop is queued (send_queue.py),
and the engine's reintegration producer (Reintegration) delivers the queue:
one crop at a time, best first, each until the verifier's RESULT for it, sent
again when its turn in the flow ends without one. Give the producer one
token: the flow then paces it.
"""

import logging
import time

import uriel

from detection_crop.crop import CLASSIFICATION_RESULT_TYPE, RAW_FRAME_TYPE, type_name

logger = logging.getLogger("detection_crop")

# How long the engine waits for an input or event before it reads its
# producer's events, sends a queued crop, and checks whether to stop. It
# bounds how long the producer takes to see its last crop's turn end.
POLL_INTERVAL_S = 0.02
# The producer that sends queued crops, as detection-crop.plugin.toml
# advertises it; the engine publishes on {engine name}.{this}.
REINTEGRATION = "reintegration"


def producer_name(engine, producer):
    """The name the engine `engine` publishes its producer `producer` on, as
    eagled names it."""
    return f"{engine}.{producer}"


class Reintegration:
    """The engine's reintegration producer: delivers the crops waiting in
    `queue`, best first, whether or not inputs come in, one at a time: the
    next goes only once the last one's turn in the flow ended (the queue
    settles it on the flow's events; see send_queue.py). Its producer has one
    token, so the flow paces it, and with only one input published at a time
    the client never replaces one before it enters the flow."""

    def __init__(self, queue):
        self.queue = queue
        # The crop published last and its input id, until its turn ends
        self._current = None
        self.published = 0

    def step(self, now, publish):
        """Publishes the best waiting crop with `publish(payload)`, which
        returns its input id, unless the crop published last is still in the
        flow. Returns the crop published, or None."""
        if self._current is not None:
            crop, id = self._current
            if crop in self.queue and crop.in_flight and crop.carriers[-1] == id:
                return None
            self._current = None  # confirmed, dropped, or put back to wait
        best = self.queue.best()
        if best is None:
            return None
        id = publish(best.payload)
        self.queue.sent(best, id, now)
        self._current = (best, id)
        self.published += 1
        logger.info(
            "reintegration: sending the crop of frame %d (score %.3f)%s; queue %d",
            best.frame_id,
            best.score,
            f", try {len(best.carriers)}" if len(best.carriers) > 1 else "",
            len(self.queue),
        )
        return best


def run(name, cropper, *, stop, on_ready=None):
    """Serves the engine `name`, cropping the frames it gets by the events of
    `cropper.detector` with the Cropper `cropper`, and its reintegration
    producer, which delivers the crops to `cropper.verifier`, following the
    events about its inputs at every engine, until the threading.Event `stop`
    is set."""
    producer = producer_name(name, REINTEGRATION)
    reintegration = Reintegration(cropper.queue)
    size = cropper.max_output_size
    with (
        uriel.Engine(name, result_type=CLASSIFICATION_RESULT_TYPE, max_output_size=size) as engine,
        uriel.Producer(producer, input_type=RAW_FRAME_TYPE, max_input_size=size) as crops,
        uriel.Subscription.producer(producer) as events,
    ):
        engine.subscribe(cropper.detector)

        def publish(payload):
            return uriel.InputId(producer, crops.session, crops.send(payload))

        logger.info(
            "engine %s ready, with producer %s; cropping frames to the %r found by %s, "
            "verified by %s",
            name,
            producer,
            cropper.class_name,
            cropper.detector,
            cropper.verifier,
        )
        if on_ready is not None:
            on_ready()
        # The input held, and when to give up on it
        held, deadline = None, None
        while not stop.is_set():
            timeout = POLL_INTERVAL_S
            if deadline is not None:
                timeout = min(timeout, max((deadline - time.monotonic_ns()) / 1e9, 0.0))
            if timeout > 0:
                engine.wait(timeout)
            while (event := engine.poll_event()) is not None:
                try:
                    cropper.on_event(event)
                except Exception:
                    logger.exception("failed to handle a %s event", event.kind)
            while (event := events.poll()) is not None:
                try:
                    cropper.queue.on_event(event, cropper.verifier)
                except Exception:
                    logger.exception("failed to handle a %s event", event.kind)
            # Each input finished leaves room for the next, with no waiting
            while True:
                if held is None:
                    held = engine.poll()
                    if held is None:
                        deadline = None
                        break
                    if held.payload_type and type_name(held.payload_type) != RAW_FRAME_TYPE:
                        logger.warning(
                            "input %d is a %r, not a RawFrame; dropping it",
                            held.id.seq,
                            held.payload_type,
                        )
                        engine.drop()
                        held = None
                        continue
                    deadline = cropper.deadline()
                outcome = cropper.outcome(held.id, deadline)
                if outcome is None:
                    break
                finish(engine, cropper, held, outcome)
                held, deadline = None, None
            try:
                reintegration.step(time.monotonic(), publish)
            except Exception:
                logger.exception("reintegration failed to send a crop")
            cropper.summarize()


def finish(engine, cropper, input, outcome):
    """Reports the outcome of the held `input`: its crop and the crop's
    result, or a drop."""
    try:
        crop, result = cropper.apply(input.id, bytes(input.data), outcome)
    except Exception:
        logger.exception("input %d: failed to crop it; dropping it", input.id.seq)
        crop, result = None, b""
    if crop is None:
        engine.drop()
        return
    engine.loan()[: len(crop.payload)] = crop.payload
    engine.replace(len(crop.payload), output_type=RAW_FRAME_TYPE, result=result)
