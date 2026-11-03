"""The engine loop of the fusion plugins.

A plugin is a handler that takes each event of the engines it subscribes to
(`on_event`), and decides each input from them: `begin(id)` starts an input,
`decide(id)` says whether to wait (a Wait, with the time to decide by if
nothing else happens) or report (a Report, with the result), and `end(id)`
finishes it. Every input is dropped, with the decided result, if any.
"""

import logging
import time

import uriel

from fusion.detection import Report

logger = logging.getLogger("fusion")

# How long the engine waits for an input or event before it checks whether to
# stop.
POLL_INTERVAL_S = 0.1


def qualify(qualifier, name):
    """`name` qualified with a daemon's IP, as eagled names its services, or
    as is if there is no qualifier."""
    return f"{qualifier}.{name}" if qualifier else name


def run(
    name,
    handler,
    subscriptions,
    *,
    result_type,
    max_output_size,
    stop,
    on_ready=None,
):
    """Serves the engine `name`, subscribed to the engines `subscriptions`,
    until the threading.Event `stop` is set."""
    with uriel.Engine(
        name, result_type=result_type, max_output_size=max_output_size
    ) as engine:
        for subscription in subscriptions:
            engine.subscribe(subscription)
        logger.info("engine %s ready; fusing %s", name, ", ".join(subscriptions))
        if on_ready is not None:
            on_ready()
        current = None
        while not stop.is_set():
            timeout = POLL_INTERVAL_S
            if current is not None:
                decision = handler.decide(current)
                if isinstance(decision, Report):
                    _report(engine, current, decision, result_type, max_output_size)
                    handler.end(current)
                    current = None
                    continue
                left = (decision.deadline_ns - time.monotonic_ns()) / 1e9
                timeout = min(timeout, max(left, 0.0))
            if timeout > 0:
                engine.wait(timeout)
            while (event := engine.poll_event()) is not None:
                try:
                    handler.on_event(event)
                except Exception:
                    logger.exception("failed to handle a %s event", event.kind)
            if current is None and (input := engine.poll()) is not None:
                current = input.id
                handler.begin(current)


def _report(engine, id, decision, result_type, max_output_size):
    """Drops the input `id`, with the decided result."""
    result = decision.result
    if result is None:
        engine.drop()
        return
    # Engines write a type's name with slashes or, as detection-qnn does, dots
    if result.payload_type and result.payload_type.replace(".", "/") != result_type:
        logger.warning(
            "input %d: %s sent a %r, not a %r",
            id.seq,
            decision.source,
            result.payload_type,
            result_type,
        )
    if len(result.data) > max_output_size:
        logger.warning(
            "input %d: the result of %s is %d bytes, over %d; dropping it",
            id.seq,
            decision.source,
            len(result.data),
            max_output_size,
        )
        engine.drop()
        return
    engine.drop(result=result.data)
