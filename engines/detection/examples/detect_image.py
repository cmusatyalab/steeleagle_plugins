"""Sends an image to a running detection engine and prints its detections.

The image can be sent several times to time the engine: the first --warmup
results are discarded and the rest are summarized as round-trip latencies.

Start the engine first (eagled normally does this), e.g. from engines/detection:

    STEELEAGLE_PLUGIN=detection.plugin.toml \
    STEELEAGLE_ENGINE_ENDPOINT=detector \
    STEELEAGLE_PLUGIN_ARGS='{"model": "yolo26n.pt"}' \
    pixi run start

Then, on the same machine:

    cd examples && uv run detect_image.py photo.jpg --engine detector
    cd examples && uv run detect_image.py photo.jpg --iterations 50 --warmup 5
"""

import argparse
import contextlib
import statistics
import sys
import time
import uuid

import uriel
from PIL import Image, ImageDraw

from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.result import result_pb2

RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"
DETECTION_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/DetectionResult"
RGB_CHANNELS = 3
# Room for the protobuf framing around the pixels
FRAME_OVERHEAD_BYTES = 4096
DEFAULT_TIMEOUT_S = 30.0
EVENT_POLL_INTERVAL_S = 0.2
FLOW_SPEC = """
[[producers]]
name = "{producer}"
tokens = 1

[[engines]]
name = "{engine}"
location = "local"

[[edges]]
from = "{producer}"
to = "{engine}"
timeout = "{timeout_s}s"
"""
FAILED_EVENTS = (uriel.EventKind.SKIPPED, uriel.EventKind.TIMED_OUT)
BOX_COLOR = "red"
DEFAULT_ITERATIONS = 1
DEFAULT_WARMUP = 0
MS_PER_S = 1000.0
# statistics.quantiles cuts the data into this many groups, so index
# P95_CUT - 1 is the 95th percentile
PERCENTILE_GROUPS = 100
P95_CUT = 95


def make_frame(image):
    """A RawFrame holding the pixels of a PIL image."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    return frame_pb2.RawFrame(
        data=rgb.tobytes(),
        h_res=width,
        v_res=height,
        channels=RGB_CHANNELS,
        id=1,
    )


@contextlib.contextmanager
def engine_session(engine_name, max_payload_bytes, timeout_s):
    """Connects a producer to the engine once and yields `detect(payload)`,
    which sends a serialized frame and returns its DetectionResult."""
    suffix = uuid.uuid4().hex[:8]
    producer_name = f"client-camera-{suffix}"
    server = uriel.Server(f"client-server-{suffix}")
    client = uriel.Client(f"client-{suffix}", f"client-server-{suffix}")
    producer = uriel.Producer(
        producer_name,
        input_type=RAW_FRAME_TYPE,
        max_input_size=max_payload_bytes + FRAME_OVERHEAD_BYTES,
    )

    def detect(payload):
        seq = producer.send(payload)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            event = events.next(EVENT_POLL_INTERVAL_S)
            if event is None or event.id.seq != seq:
                continue
            if event.kind == uriel.EventKind.RESULT:
                # No detections serialize to zero bytes, which carry no payload type
                assert event.payload_type in (DETECTION_RESULT_TYPE, "")
                return result_pb2.DetectionResult.FromString(event.result)
            if event.kind in FAILED_EVENTS:
                raise RuntimeError(f"engine {engine_name!r}: {event.kind.name}")
        raise TimeoutError(f"no result from engine {engine_name!r} in {timeout_s}s")

    try:
        events = client.add_producer_with_events(producer_name)
        server.add_engine(engine_name)
        client.add_flow(
            f"flow-{suffix}",
            FLOW_SPEC.format(
                producer=producer_name, engine=engine_name, timeout_s=int(timeout_s)
            ),
        )
        yield detect
    finally:
        producer.close()
        client.close()
        server.close()


def summarize(latencies_s):
    """Latency statistics, in milliseconds, of a list of times in seconds."""
    ms = [t * MS_PER_S for t in latencies_s]
    many = len(ms) > 1
    cuts = (
        statistics.quantiles(ms, n=PERCENTILE_GROUPS, method="inclusive")
        if many
        else ms
    )
    return {
        "mean": statistics.fmean(ms),
        "median": statistics.median(ms),
        "stdev": statistics.stdev(ms) if many else 0.0,
        "min": min(ms),
        "max": max(ms),
        "p95": cuts[P95_CUT - 1] if many else ms[0],
    }


def annotate(image, result, path):
    """Saves `image` with the boxes of `result` drawn on it."""
    annotated = image.convert("RGB")
    draw = ImageDraw.Draw(annotated)
    width, height = annotated.size
    for d in result.detections:
        box = d.bbox
        left, top = box.x_min * width, box.y_min * height
        draw.rectangle(
            (left, top, box.x_max * width, box.y_max * height),
            outline=BOX_COLOR,
            width=2,
        )
        draw.text((left, top), f"{d.class_name} {d.score:.2f}", fill=BOX_COLOR)
    annotated.save(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("image", help="path of the picture to send")
    parser.add_argument(
        "--engine", default="detector", help="name the engine is served as"
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument(
        "--iterations",
        type=int,
        default=DEFAULT_ITERATIONS,
        help="total times to send the image, including the warmup",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=DEFAULT_WARMUP,
        help="results to discard from the start, e.g. while the model warms up",
    )
    parser.add_argument("--output", help="save a copy of the image with boxes drawn")
    args = parser.parse_args()

    if args.warmup < 0 or args.iterations <= args.warmup:
        parser.error("--iterations must exceed --warmup, which cannot be negative")

    image = Image.open(args.image)
    payload = make_frame(image).SerializeToString()
    latencies = []
    with engine_session(args.engine, len(payload), args.timeout) as detect:
        for i in range(args.iterations):
            started = time.perf_counter()
            result = detect(payload)
            elapsed = time.perf_counter() - started
            if i >= args.warmup:
                latencies.append(elapsed)

    if not result.detections:
        print("no detections")
    for d in result.detections:
        b = d.bbox
        print(
            f"{d.class_name:<16} {d.score:.2f}  "
            f"x[{b.x_min:.3f},{b.x_max:.3f}] y[{b.y_min:.3f},{b.y_max:.3f}]"
        )
    if args.output:
        annotate(image, result, args.output)
        print(f"wrote {args.output}", file=sys.stderr)
    if args.iterations > 1:
        stats = summarize(latencies)
        print(
            f"\n{len(latencies)} timed runs after {args.warmup} discarded (ms): "
            + "  ".join(f"{name} {value:.1f}" for name, value in stats.items())
            + f"  ({MS_PER_S / stats['mean']:.1f} runs/s)"
        )


if __name__ == "__main__":
    main()
