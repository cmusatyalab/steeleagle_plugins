"""Uriel engine: measures the end-to-end latency, throughput and frame drops
of the EncodedFrames reaching it. Latency is the time the frame arrives minus
the timestamp the device stamped on it (the fake drone stamps each frame as
it publishes it; the H.264 encoder carries the stamp through), so it assumes
the clocks of the device and this host are synchronized.

A terminal sink by default. Logs a summary every report_interval_seconds of
arriving frames, and writes one CSV row per frame if csv_path is set. The
summary is driven by arriving frames, so none is logged while none arrive.
"""
import csv
import logging
import time

import uriel
from google.protobuf.message import DecodeError

from steeleagle_protocol.v1.messages.frame import frame_pb2
from window import NANOS_PER_MILLI, NANOS_PER_SECOND, DropCounter, Window

logger = logging.getLogger("metrics")

CSV_HEADER = ("frame_id", "send_ts_ns", "recv_ts_ns", "latency_ms", "bytes")


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

    csv_file = open(args["csv_path"], "w", newline="") if args["csv_path"] else None
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
                frame = frame_pb2.EncodedFrame()
                try:
                    frame.ParseFromString(bytes(input.data))
                except DecodeError as e:
                    logger.warning("input %d: not an EncodedFrame (%s); dropping", input.id.seq, e)
                    engine.drop()
                    continue

                send_ns = frame.timestamp.ToNanoseconds()
                latency_ns = recv_ns - send_ns
                size = len(input.data)
                missing = drops.observe(frame.id)
                window.add(latency_ns, size, missing)
                total.add(latency_ns, size, missing)
                if writer:
                    writer.writerow((frame.id, send_ns, recv_ns, latency_ns / NANOS_PER_MILLI, size))

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
