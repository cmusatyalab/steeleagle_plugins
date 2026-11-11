"""The engine loop: each frame is timed as it is taken, read and dropped,
with no result."""

import logging
import time

import uriel
from google.protobuf.message import DecodeError
from steeleagle_protocol.v1.messages.frame import frame_pb2

logger = logging.getLogger("led_clock")

RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"
# How long the engine waits for an input before it checks whether to stop, or
# to report.
POLL_INTERVAL_S = 0.1
# It reports no results, so it needs no room for them.
MAX_OUTPUT_SIZE = 1


def parse_frame(input):
    """The RawFrame of `input`, or None if it is not one."""
    if input.payload_type and input.payload_type.replace(".", "/") != RAW_FRAME_TYPE:
        return None
    try:
        return frame_pb2.RawFrame.FromString(bytes(input.data))
    except DecodeError:
        return None


def run(name, log, *, stop, on_ready=None):
    """Serves the engine `name`, feeding the LatencyLog `log` the frames it
    gets, until the threading.Event `stop` is set; then closes the log, which
    reports the whole run."""
    try:
        _serve(name, log, stop=stop, on_ready=on_ready)
    finally:
        log.close()


def _serve(name, log, *, stop, on_ready):
    with uriel.Engine(name, max_output_size=MAX_OUTPUT_SIZE) as engine:
        logger.info("engine %s ready", name)
        if on_ready is not None:
            on_ready()
        while not stop.is_set():
            engine.wait(POLL_INTERVAL_S)
            while (input := engine.poll()) is not None:
                # Timed before parsing, so parsing is not counted
                recv_ns = time.time_ns()
                seq = input.id.seq
                try:
                    frame = parse_frame(input)
                finally:
                    engine.drop()
                if frame is None:
                    logger.warning("input %d: a %r is not a RawFrame", seq, input.payload_type)
                try:
                    log.on_frame(seq, frame, recv_ns)
                except Exception:
                    logger.exception("failed to read input %d", seq)
            log.report()
