"""The engine loop: each frame is read and dropped at once, with no result,
so frames can wait on the detector while later ones arrive."""

import logging
import time

import uriel

from detection_log.log import parse_frame

logger = logging.getLogger("detection_log")

# How long the engine waits for an input or event before it checks whether to
# stop.
POLL_INTERVAL_S = 0.1
# It reports no results, so it needs no room for them.
MAX_OUTPUT_SIZE = 1


def run(name, log, *, stop, on_ready=None):
    """Serves the engine `name`, feeding the DetectionLog `log` the frames it
    gets and the events of `log.detector`, until the threading.Event `stop` is
    set. The log's saver is closed when the engine stops."""
    try:
        with uriel.Engine(name, max_output_size=MAX_OUTPUT_SIZE) as engine:
            engine.subscribe(log.detector)
            logger.info("engine %s ready; logging %s", name, log.detector)
            if on_ready is not None:
                on_ready()
            next_deadline = None
            while not stop.is_set():
                timeout = POLL_INTERVAL_S
                if next_deadline is not None:
                    left = (next_deadline - time.monotonic_ns()) / 1e9
                    timeout = min(timeout, max(left, 0.0))
                if timeout > 0:
                    engine.wait(timeout)
                while (event := engine.poll_event()) is not None:
                    try:
                        log.on_event(event)
                    except Exception:
                        logger.exception("failed to handle a %s event", event.kind)
                while (input := engine.poll()) is not None:
                    id = input.id
                    try:
                        frame = parse_frame(input)
                    finally:
                        engine.drop()
                    if frame is None:
                        logger.warning(
                            "input %d: a %r is not a RawFrame", id.seq, input.payload_type
                        )
                    log.on_frame(id, frame)
                next_deadline = log.expire()
    finally:
        log.saver.close()
