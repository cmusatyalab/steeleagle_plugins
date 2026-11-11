"""The engine loop: each frame is held until the detector's result for it is
in, then replaced with its crop, or dropped. An engine holds one input at a
time, so later frames wait at the server meanwhile; a replaced input keeps
its id."""

import logging
import time

import uriel

from detection_crop.crop import RAW_FRAME_TYPE, type_name

logger = logging.getLogger("detection_crop")

# How long the engine waits for an input or event before it checks whether to
# stop.
POLL_INTERVAL_S = 0.1


def run(name, cropper, *, stop, on_ready=None):
    """Serves the engine `name`, cropping the frames it gets by the events of
    `cropper.detector` with the Cropper `cropper`, until the threading.Event
    `stop` is set."""
    with uriel.Engine(name, max_output_size=cropper.max_output_size) as engine:
        engine.subscribe(cropper.detector)
        logger.info(
            "engine %s ready; cropping frames to the %r found by %s",
            name,
            cropper.class_name,
            cropper.detector,
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
            cropper.summarize()


def finish(engine, cropper, input, outcome):
    """Reports the outcome of the held `input`: its crop, or a drop."""
    try:
        out = cropper.apply(input.id, bytes(input.data), outcome)
    except Exception:
        logger.exception("input %d: failed to crop it; dropping it", input.id.seq)
        out = None
    if out is None:
        engine.drop()
        return
    engine.loan()[: len(out)] = out
    engine.replace(len(out), output_type=RAW_FRAME_TYPE)
