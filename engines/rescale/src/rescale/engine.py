"""The engine loop: each RawFrame is replaced with its rescaled copy, or sent
on unchanged if it is already at the target resolution. A replaced input
keeps its id."""

import logging

import uriel

from rescale.rescale import RAW_FRAME_TYPE, max_output_size, rescale

logger = logging.getLogger("rescale")

# How long the engine waits for an input before it checks whether to stop.
POLL_INTERVAL_S = 0.1


def run(name, dimensions, *, stop, on_ready=None):
    """Serves the engine `name`, rescaling frames to `dimensions` (width,
    height), until the threading.Event `stop` is set."""
    limit = max_output_size(dimensions)
    with uriel.Engine(name, max_output_size=limit) as engine:
        logger.info("engine %s ready; rescaling frames to %dx%d", name, *dimensions)
        if on_ready is not None:
            on_ready()
        while not stop.is_set():
            input = engine.next(POLL_INTERVAL_S)
            if input is None:
                continue
            if input.payload_type.replace(".", "/") != RAW_FRAME_TYPE:
                logger.warning(
                    "input %d is a %r, not a RawFrame; sending it on unchanged",
                    input.id.seq,
                    input.payload_type,
                )
                engine.forward()
                continue
            try:
                out = rescale(bytes(input.data), dimensions)
            except Exception:
                logger.exception("input %d: failed to rescale it; dropping it", input.id.seq)
                engine.drop()
                continue
            if out is None:
                engine.forward()
                continue
            if len(out) > limit:
                logger.warning(
                    "input %d rescaled to %d bytes, over %d; dropping it",
                    input.id.seq,
                    len(out),
                    limit,
                )
                engine.drop()
                continue
            engine.loan()[: len(out)] = out
            engine.replace(len(out), output_type=RAW_FRAME_TYPE)
