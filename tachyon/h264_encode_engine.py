"""Uriel engine: encodes the raw YUV420 frames patchcore-score forwards
(anomalous frames only) with H.264, and emits each as an EncodedVideoFrame
for h264-decode on the remote side.

Frames reach the decoder only if Uriel carries them there: the crossing to
the remote server may skip a frame for a newer one, or time it out. Each
P-frame needs the frames before it, so this engine subscribes to
h264-decode's events and forces a keyframe whenever a frame it encoded since
its last keyframe did not reach the decoder cleanly.
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

H264_ENCODE_ENGINE = "h264-encode"
H264_DECODE_ENGINE = "h264-decode"
DEFAULT_ENCODER = "x264enc tune=zerolatency speed-preset=ultrafast pass=quant quantizer=18 key-int-max=300"
ENCODED_TYPE = h264_pb2.EncodedVideoFrame.DESCRIPTOR.full_name


class KeyframePolicy:
    """Decides when the next frame must be a keyframe: after a frame the
    decoder will not get, which breaks the chain of references from the
    last keyframe."""

    def __init__(self):
        self.needed = True
        self._keyframe_seq = None

    def emitted(self, seq, keyframe):
        if keyframe:
            self.needed = False
            self._keyframe_seq = seq

    def decoder_event(self, seq, result_bytes):
        ok = False
        if result_bytes:
            try:
                r = h264_pb2.DecodeResult()
                r.ParseFromString(bytes(result_bytes))
                ok = r.decoded and not r.keyframe_needed
            except Exception:
                ok = False
        if not ok:
            self._require_if_since_keyframe(seq)

    def _require_if_since_keyframe(self, seq):
        if self._keyframe_seq is None or seq is None or seq >= self._keyframe_seq:
            self.needed = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--encoder",
        default=DEFAULT_ENCODER,
        help="H.264 encoder, in gst-launch syntax. On the Tachyon: "
        "'qtic2venc control-rate=5 b-frames=0'",
    )
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--max-output-size", type=int, default=MAX_FRAME_SIZE)
    ap.add_argument("--timeout-ms", type=int, default=1000)
    ap.add_argument("--log-frames", action="store_true")
    args = ap.parse_args()

    uses_iframe_action = "qtic2venc" in args.encoder
    encoder_desc = args.encoder if " name=" in args.encoder else f"{args.encoder} name=enc"

    pipeline = Pipeline(
        f"appsrc name=src ! videoconvert ! {encoder_desc} "
        f"! h264parse config-interval=-1 "
        f"! video/x-h264,stream-format=byte-stream,alignment=au ! appsink name=sink",
        f"video/x-raw,format=RGB,width={FRAME_W},height={FRAME_H},framerate={args.fps}/1",
        uses_iframe_action=uses_iframe_action,
    )

    policy = KeyframePolicy()
    start = time.monotonic()

    with uriel.Engine(H264_ENCODE_ENGINE, max_output_size=args.max_output_size) as engine:
        engine.subscribe(H264_DECODE_ENGINE)
        print(f"engine {H264_ENCODE_ENGINE} ready; watching {H264_DECODE_ENGINE}", flush=True)

        frame_number = 0
        try:
            while True:
                engine.wait(0.1)

                while (event := engine.poll_event()) is not None:
                    seq = event.id.seq if getattr(event, "id", None) is not None else None
                    policy.decoder_event(seq, getattr(event, "result", b"") or b"")

                input = engine.poll()
                if input is None:
                    continue

                started = time.perf_counter()
                data = bytes(input.data)
                if len(data) != RGB_FRAME_SIZE:
                    print(
                        f"discarding input {input.id.seq}: {len(data)} bytes, "
                        f"not a {FRAME_W}x{FRAME_H} RGB frame",
                        flush=True,
                    )
                    engine.drop()
                    continue

                forced = policy.needed
                pts_ns = int((time.monotonic() - start) * 1e9)
                if forced:
                    pipeline.force_keyframe()

                t_push = time.perf_counter()
                pipeline.push(data, pts_ns)
                t_pushed = time.perf_counter()
                sample, dropped_late = pipeline.pull(pts_ns, args.timeout_ms)
                t_pulled = time.perf_counter()
                print(
                    f"  push={(t_pushed - t_push) * 1e3:.2f}ms pull={(t_pulled - t_pushed) * 1e3:.2f}ms",
                    flush=True,
                )

                if dropped_late:
                    policy.needed = True

                number = frame_number
                frame_number += 1
                if sample is None:
                    print(f"frame {number} took over {args.timeout_ms} ms to encode; discarding it", flush=True)
                    policy.needed = True
                    engine.drop()
                    continue

                buf = sample.get_buffer()
                keyframe = not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
                ok, mapinfo = buf.map(Gst.MapFlags.READ)
                try:
                    encoded_bytes = bytes(mapinfo.data)
                finally:
                    buf.unmap(mapinfo)

                out = h264_pb2.EncodedVideoFrame()
                out.frame_number = number
                out.keyframe = keyframe
                out.data = encoded_bytes
                payload = out.SerializeToString()
                if len(payload) > args.max_output_size:
                    print(f"frame {number} is {len(payload)} bytes, over --max-output-size; discarding it", flush=True)
                    policy.needed = True
                    engine.drop()
                    continue

                out_buf = engine.loan()
                out_buf[: len(payload)] = payload
                engine.replace(len(payload), output_type=ENCODED_TYPE, result=b"")
                policy.emitted(input.id.seq, keyframe)
                if args.log_frames:
                    print(
                        f"frame {number} input {input.id.seq} keyframe={keyframe} "
                        f"bytes={len(payload)} ms={(time.perf_counter() - started) * 1e3:.2f}",
                        flush=True,
                    )
        finally:
            pipeline.stop()


if __name__ == "__main__":
    main()
