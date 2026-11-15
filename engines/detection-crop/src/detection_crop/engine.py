"""The engine loop: each frame is held until the detector's result for it is
in, then replaced with its crop and the crop's ClassificationResult, or
dropped. An engine holds one input at a time, so later frames wait at the
server meanwhile; a replaced input keeps its id.

Each crop sent is queued too (send_queue.py), until the verifier's RESULT for
it, and the engine's reintegration producer (Reintegration) sends the queue's
waiting crops, best first, as the flow has room for them. So a crop not yet
confirmed (lost, timed out, or found while the link was down) still reaches
the verifier when no frames come in, or when every frame's token is taken.
"""

import logging
import time

import uriel

from detection_crop.crop import CLASSIFICATION_RESULT_TYPE, RAW_FRAME_TYPE, type_name

logger = logging.getLogger("detection_crop")

# How long the engine waits for an input or event before it reads its
# producer's events, sends a queued crop, and checks whether to stop. It
# bounds how long the producer takes to see its last crop admitted.
POLL_INTERVAL_S = 0.02
# The producer that sends queued crops, as detection-crop.plugin.toml
# advertises it; the engine publishes on {engine name}.{this}.
REINTEGRATION = "reintegration"


def producer_name(engine, producer):
    """The name the engine `engine` publishes its producer `producer` on, as
    eagled names it."""
    return f"{engine}.{producer}"


class Reintegration:
    """The engine's reintegration producer: sends the crops waiting in
    `queue`, best first, whether or not inputs come in.

    Uriel's client keeps only a producer's newest input until the flow has
    room for it, and an input it replaces there never enters the flow, with no
    event. So one crop at a time waits to be admitted: the next goes once an
    event (`on_event`) shows the last one entered the flow, unless a better
    crop turns up, which replaces it, and it waits again. A crop admitted is
    in flight like any other until the verifier's events settle it
    (send_queue.py)."""

    def __init__(self, queue):
        self.queue = queue
        # The crop published last and its input id, until admitted
        self._pending = None
        self.published = 0

    def on_event(self, event):
        """Takes an event about the producer's inputs, at any engine, or of
        the verifier: any event about the crop waiting means it was
        admitted."""
        if self._pending is not None and event.id == self._pending[1]:
            self._pending = None

    def step(self, now, publish):
        """Publishes the best waiting crop with `publish(payload)`, which
        returns its input id, unless the crop published last still waits to
        be admitted and is no worse. Returns the crop published, or None."""
        best = self.queue.best(now)
        if self._pending is not None:
            crop, id = self._pending
            if crop not in self.queue or not crop.in_flight or crop.carriers[-1] != id:
                self._pending = None  # confirmed, dropped, or put back to wait
            elif best is None or best.priority <= crop.priority:
                return None
            else:
                # The better crop replaces it at the client, so it never leaves
                self.queue.unsend(crop, id)
                self._pending = None
                best = self.queue.best(now)
        if best is None:
            return None
        id = publish(best.payload)
        self.queue.sent(best, id, now)
        self._pending = (best, id)
        self.published += 1
        logger.info(
            "reintegration: sending the crop of frame %d (score %.3f); queue %d (%d in flight)",
            best.frame_id,
            best.score,
            len(self.queue),
            self.queue.in_flight,
        )
        return best


def run(name, cropper, *, stop, on_ready=None):
    """Serves the engine `name`, cropping the frames it gets by the events of
    `cropper.detector` with the Cropper `cropper`, and its reintegration
    producer, which sends the crops `cropper.verifier` doesn't have yet,
    until the threading.Event `stop` is set."""
    producer = producer_name(name, REINTEGRATION)
    reintegration = Reintegration(cropper.queue)
    size = cropper.max_output_size
    with (
        uriel.Engine(name, result_type=CLASSIFICATION_RESULT_TYPE, max_output_size=size) as engine,
        uriel.Producer(producer, input_type=RAW_FRAME_TYPE, max_input_size=size) as crops,
        uriel.Subscription.producer(producer) as admissions,
    ):
        engine.subscribe(cropper.detector)
        engine.subscribe(cropper.verifier)

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
                    reintegration.on_event(event)
                except Exception:
                    logger.exception("failed to handle a %s event", event.kind)
            while (event := admissions.poll()) is not None:
                reintegration.on_event(event)
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
