"""Uriel engine: decodes h264-encode's EncodedVideoFrames back to raw
YUV420 bytes, in the exact shape verifier_engine.py already expects
(output_type="", fixed FRAME_W x FRAME_H I420). Unchanged, verifier needs no
edits.

A P-frame decodes correctly only after every frame before it, back to a
keyframe. After a gap in the frame numbers, this drops frames until the next
keyframe and reports keyframe_needed; h264-encode, which subscribes to this
engine's events, then makes one.
"""
import argparse
import time

import uriel

import h264_pb2
from common import MAX_FRAME_SIZE
from h264_common import FRAME_H, FRAME_W, RGB_FRAME_SIZE, Pipeline

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

H264_DECODE_ENGINE = "h264-decode"
DEFAULT_DECODER = "avdec_h264 max-threads=1"
ENCODED_TYPE = h264_pb2.EncodedVideoFrame.DESCRIPTOR.full_name


def _result(decoded, keyframe_needed):
    r = h264_pb2.DecodeResult()
    r.decoded = decoded
    r.keyframe_needed = keyframe_needed
    return r.SerializeToString()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--decoder", default=DEFAULT_DECODER)
    ap.add_argument("--timeout-ms", type=int, default=1000)
    ap.add_argument("--log-frames", action="store_true")
    args = ap.parse_args()

    pipeline = Pipeline(
        f"appsrc name=src ! h264parse ! {args.decoder} ! videoconvert "
        f"! video/x-raw,format=RGB,width={FRAME_W},height={FRAME_H} ! appsink name=sink",
        "video/x-h264,stream-format=byte-stream,alignment=au",
    )

    decoded_result = _result(True, False)
    dropped_result = _result(False, False)
    keyframe_needed_result = _result(False, True)

    expected = None  # the frame_number that must come next to decode
    start = time.monotonic()

    with uriel.Engine(H264_DECODE_ENGINE, max_output_size=MAX_FRAME_SIZE) as engine:
        print(f"engine {H264_DECODE_ENGINE} ready", flush=True)
        for input in engine:
            started = time.perf_counter()
            if input.payload_type != ENCODED_TYPE:
                print(f"discarding input {input.id.seq}: not an {ENCODED_TYPE}", flush=True)
                engine.drop(result=dropped_result)
                continue

            frame = h264_pb2.EncodedVideoFrame()
            frame.ParseFromString(bytes(input.data))
            number = frame.frame_number

            if not frame.keyframe and expected != number:
                expected = None
                if args.log_frames:
                    print(f"frame {number} input {input.id.seq} follows a lost frame; waiting for a keyframe", flush=True)
                engine.drop(result=keyframe_needed_result)
                continue
            # The decoder keeps this frame as a reference whether or not its
            # output comes in time, so the next one decodes either way.
            expected = number + 1

            pts_ns = int((time.monotonic() - start) * 1e9)
            pipeline.push(frame.data, pts_ns)
            sample, _dropped_late = pipeline.pull(pts_ns, args.timeout_ms)
            if sample is None:
                print(f"frame {number} took over {args.timeout_ms} ms to decode; discarding it", flush=True)
                engine.drop(result=dropped_result)
                continue

            buf = sample.get_buffer()
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            try:
                pixels = bytes(mapinfo.data)
            finally:
                buf.unmap(mapinfo)

            if len(pixels) != RGB_FRAME_SIZE:
                print(f"frame {number} decoded to {len(pixels)} bytes, not {RGB_FRAME_SIZE}; discarding it", flush=True)
                engine.drop(result=dropped_result)
                continue

            out = engine.loan()
            out[: len(pixels)] = pixels
            engine.replace(len(pixels), output_type="", result=decoded_result)
            if args.log_frames:
                print(
                    f"frame {number} input {input.id.seq} keyframe={frame.keyframe} "
                    f"ms={(time.perf_counter() - started) * 1e3:.2f}",
                    flush=True,
                )


if __name__ == "__main__":
    main()
