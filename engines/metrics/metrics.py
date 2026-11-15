"""Uriel engine: measures the end-to-end latency, throughput and frame drops
of the frames (EncodedFrames or RawFrames) reaching it. Latency is the time
the frame arrives minus
the timestamp the device stamped on it (the fake drone stamps each frame as
it publishes it; the H.264 encoder carries the stamp through), so it assumes
the clocks of the device and this host are synchronized.

A terminal sink by default. Logs a summary every report_interval_seconds of
arriving frames, and writes one CSV row per frame if csv_path is set. The
summary is driven by arriving frames, so none is logged while none arrive.

Each CSV row also has the frame's capture timestamp (send_ts_ns), which
encoders and decoders carry through while they renumber ids, so a row can be
matched to the source frame the fake drone logged with that timestamp.
"""
import csv
import logging
import os
import time
from pathlib import Path

import uriel
from google.protobuf.message import DecodeError

from steeleagle_protocol.v1.messages.frame import frame_pb2
from window import NANOS_PER_MILLI, NANOS_PER_SECOND, DropCounter, Window

logger = logging.getLogger("metrics")

CSV_HEADER = ("frame_id", "send_ts_ns", "recv_ts_ns", "latency_ms", "bytes", "kind", "width", "height",
              "producer", "seq")

ENCODED_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/EncodedFrame"
RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"


def csv_file_path(path):
    """`path`, or, if it is relative, that path in the daemon's files
    directory (STEELEAGLE_FILES), so that it can be collected as a result.
    Its directory is created."""
    path = Path(path)
    files = os.environ.get("STEELEAGLE_FILES")
    if not path.is_absolute() and files:
        path = Path(files) / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def parse_frame(input):
    """The frame in `input`, as (frame, kind, width, height): an EncodedFrame
    (kind "encoded", its size unknown, 0) or a RawFrame ("raw"), by the
    input's payload type, or, if it has none, whichever parses. Raises
    DecodeError if it is neither."""
    payload_type = (getattr(input, "payload_type", "") or "").replace(".", "/")
    data = bytes(input.data)
    if payload_type == RAW_FRAME_TYPE:
        frame = frame_pb2.RawFrame.FromString(data)
        return frame, "raw", frame.h_res, frame.v_res
    if payload_type == ENCODED_FRAME_TYPE:
        return frame_pb2.EncodedFrame.FromString(data), "encoded", 0, 0
    # Untyped: both messages keep their bytes in field 2, so either parses;
    # a RawFrame is told apart by its pixels filling its stated size
    raw = frame_pb2.RawFrame.FromString(data)
    if raw.data and len(raw.data) == raw.h_res * raw.v_res * max(raw.channels, 1):
        return raw, "raw", raw.h_res, raw.v_res
    frame = frame_pb2.EncodedFrame.FromString(data)
    if not frame.encoded_data:
        raise DecodeError("neither an EncodedFrame nor a RawFrame")
    return frame, "encoded", 0, 0


def log_summary(label, summary):
    logger.info(
        "%s: %d frames, %d lost (%.1f%%), %.1f fps, %.2f Mbps; latency ms "
        "min %.1f mean %.1f p50 %.1f p95 %.1f p99 %.1f max %.1f",
        label, summary["frames"], summary["lost"], summary["loss_pct"], summary["fps"],
        summary["mbps"], summary["min_ms"], summary["mean_ms"], summary["p50_ms"],
        summary["p95_ms"], summary["p99_ms"], summary["max_ms"])


def run_engine(name, args):
    """Runs the metrics engine as `name`. `args` has every arg of
    metrics.plugin.toml."""
    interval_ns = int(args["report_interval_seconds"] * NANOS_PER_SECOND)
    drops = DropCounter()
    window = Window()
    total = Window()
    started_ns = window_started_ns = time.time_ns()

    csv_file = open(csv_file_path(args["csv_path"]), "w", newline="") if args["csv_path"] else None
    writer = csv.writer(csv_file) if csv_file else None
    if writer:
        writer.writerow(CSV_HEADER)

    try:
        with uriel.Engine(name, max_output_size=args["max_output_size"]) as engine:
            logger.info("engine %s ready; reporting every %ss%s", name,
                        args["report_interval_seconds"],
                        f", per-frame rows to {args['csv_path']}" if csv_file else "")
            for input in engine:
                recv_ns = time.time_ns()  # Before parsing, so it is not counted.
                try:
                    frame, kind, width, height = parse_frame(input)
                except DecodeError as e:
                    logger.warning("input %d: not a frame (%s); dropping", input.id.seq, e)
                    engine.drop()
                    continue

                send_ns = frame.timestamp.ToNanoseconds()
                latency_ns = recv_ns - send_ns
                size = len(input.data)
                missing = drops.observe(frame.id)
                window.add(latency_ns, size, missing)
                total.add(latency_ns, size, missing)
                if writer:
                    writer.writerow((frame.id, send_ns, recv_ns, latency_ns / NANOS_PER_MILLI, size,
                                     kind, width, height, input.id.producer, input.id.seq))

                if recv_ns - window_started_ns >= interval_ns:
                    log_summary("window", window.summary(recv_ns - window_started_ns))
                    window, window_started_ns = Window(), recv_ns
                    if csv_file:
                        csv_file.flush()

                if args["forward_frames"]:
                    engine.forward()
                else:
                    engine.drop()
    finally:
        if total.latencies_ns:
            log_summary("total", total.summary(time.time_ns() - started_ns))
            if drops.restarts:
                logger.warning("frame ids went backwards %d times (sender restarted or frames reordered)",
                               drops.restarts)
        if csv_file:
            csv_file.close()
