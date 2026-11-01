"""The engine loop both storage plugins run.

A plugin is a handler with two methods. `on_event(event)` takes each event of
the engines it subscribes to. `on_input(input)` takes each input and returns
the StoredFrame to send on in its place, or None to drop it. An engine can
only send something on as the outcome of an input, so stored frames go out
one per input, at the rate inputs arrive. A replaced input keeps its id, so
the events of the engines downstream name the input a stored frame went out
in place of, not the stored frame's own id.
"""

import logging

import uriel

logger = logging.getLogger("storage")

# How long the engine waits for an input or event before it checks whether to
# stop.
POLL_INTERVAL_S = 0.1


def qualify(qualifier, name):
    """`name` qualified with a daemon's IP, as eagled names its services, or
    as is if there is no qualifier."""
    return f"{qualifier}.{name}" if qualifier else name


def run(name, handler, subscriptions, *, max_output_size, stop, on_ready=None):
    """Serves the engine `name`, subscribed to the engines `subscriptions`,
    until the threading.Event `stop` is set."""
    with uriel.Engine(name, max_output_size=max_output_size) as engine:
        for subscription in subscriptions:
            engine.subscribe(subscription)
        logger.info("engine %s ready; watching %s", name, ", ".join(subscriptions))
        if on_ready is not None:
            on_ready()
        while not stop.is_set():
            engine.wait(POLL_INTERVAL_S)
            while (event := engine.poll_event()) is not None:
                try:
                    handler.on_event(event)
                except Exception:
                    logger.exception("failed to handle a %s event", event.kind)
            input = engine.poll()
            if input is None:
                continue
            try:
                out = handler.on_input(input)
            except Exception:
                logger.exception("input %d: failed to handle it", input.id.seq)
                out = None
            if out is None or len(out.data) > max_output_size:
                if out is not None:
                    logger.warning(
                        "frame %d is %d bytes, over max_output_size; not sending it",
                        out.key.seq,
                        len(out.data),
                    )
                engine.drop()
                continue
            buf = engine.loan()
            buf[: len(out.data)] = out.data
            engine.replace(len(out.data), output_type=out.payload_type)
