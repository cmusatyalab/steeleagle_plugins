"""Uriel engines that carry the pipeline's anomalous frames across the
network as H.264: the encoder replaces each raw RGB24 frame patchcore-score
forwards with an EncodedVideoFrame, and the decoder, on the remote side,
replaces each EncodedVideoFrame with the raw RGB24 frame it decodes to.

Frames reach the decoder only if Uriel carries them there: the crossing to
the remote server may skip a frame for a newer one, or time it out. Each
P-frame needs the frames before it, so the encoder subscribes to the remote
decoder's events and forces a keyframe whenever a frame it encoded since its
last keyframe did not reach the decoder cleanly.

Each producer gets an H.264 stream of its own. The encoder numbers the
frames of a stream in EncodedVideoFrame.frame_number, so the decoder can
tell when one was lost.
"""
import logging
import time

import uriel
from google.protobuf.message import DecodeError

import h264_pb2
from common import FRAME_H, FRAME_W, RGB_FRAME_SIZE, Gst, Pipeline

logger = logging.getLogger("h264")

ENCODED_FRAME_TYPE = h264_pb2.EncodedVideoFrame.DESCRIPTOR.full_name


def _result(decoded, keyframe_needed):
    r = h264_pb2.DecodeResult()
    r.decoded = decoded
    r.keyframe_needed = keyframe_needed
    return r.SerializeToString()


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

    def decoder_result(self, seq, result_bytes):
        ok = False
        if result_bytes:
            try:
                r = h264_pb2.DecodeResult.FromString(bytes(result_bytes))
                ok = r.decoded and not r.keyframe_needed
            except DecodeError:
                pass
        if not ok:
            self.lost(seq)

    def lost(self, seq):
        """The frame `seq` did not reach the decoder cleanly, which breaks
        the stream if it was encoded since the last keyframe."""
        k = self._keyframe_seq
        if k is None or seq is None or seq >= k:
            self.needed = True


class EncodeStream:
    """The H.264 stream of one producer."""

    def __init__(self, encoder, fps):
        uses_iframe_action = "qtic2venc" in encoder
        encoder_desc = encoder if " name=" in encoder else f"{encoder} name=enc"
        self.pipeline = Pipeline(
            f"appsrc name=src ! videoconvert ! {encoder_desc} "
            f"! h264parse config-interval=-1 "
            f"! video/x-h264,stream-format=byte-stream,alignment=au ! appsink name=sink",
            f"video/x-raw,format=RGB,width={FRAME_W},height={FRAME_H},framerate={fps}/1",
            uses_iframe_action=uses_iframe_action,
        )
        self.policy = KeyframePolicy()
        self.next_number = 0
        self.start = time.monotonic()


def _decoder_event(streams, event):
    if event.kind in (uriel.EventKind.LOST, uriel.EventKind.DISCONNECTED):
        # Which frames the lost events were about is unknown
        for stream in streams.values():
            stream.policy.needed = True
        return
    stream = streams.get(event.id.producer)
    if stream is None:
        return
    if event.kind in (uriel.EventKind.SKIPPED, uriel.EventKind.TIMED_OUT):
        stream.policy.lost(event.id.seq)
    elif event.kind == uriel.EventKind.RESULT:
        stream.policy.decoder_result(event.id.seq, getattr(event, "result", b"") or b"")


def _encode_frame(engine, streams, input, args):
    started = time.perf_counter()
    data = bytes(input.data)
    if len(data) != RGB_FRAME_SIZE:
        logger.warning(
            "discarding input %d: %d bytes, not a %dx%d RGB frame",
            input.id.seq, len(data), FRAME_W, FRAME_H,
        )
        engine.drop()
        return

    producer = input.id.producer
    stream = streams.get(producer)
    if stream is None:
        logger.info("encoding %s", producer)
        stream = streams[producer] = EncodeStream(args["encoder"], args["fps"])
    pipeline, policy = stream.pipeline, stream.policy

    pts_ns = int((time.monotonic() - stream.start) * 1e9)
    if policy.needed:
        pipeline.force_keyframe()
    pipeline.push(data, pts_ns)
    sample, dropped_late = pipeline.pull(pts_ns, args["timeout_ms"])
    if dropped_late:
        policy.needed = True

    number = stream.next_number
    if sample is None:
        logger.warning("frame %d of %s took over %d ms to encode; discarding it", number, producer, args["timeout_ms"])
        policy.needed = True
        engine.drop()
        return

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
    if len(payload) > args["max_output_size"]:
        logger.warning("frame %d of %s is %d bytes, over max_output_size; discarding it", number, producer, len(payload))
        policy.needed = True
        engine.drop()
        return

    out_buf = engine.loan()
    out_buf[: len(payload)] = payload
    engine.replace(len(payload), output_type=ENCODED_FRAME_TYPE)
    stream.next_number += 1
    policy.emitted(input.id.seq, keyframe)
    if args["log_frames"]:
        logger.info(
            "frame %d of %s input %d keyframe=%s bytes=%d ms=%.2f",
            number, producer, input.id.seq, keyframe, len(payload),
            (time.perf_counter() - started) * 1e3,
        )


def encode(name, args, remote_qualifier):
    """Runs the encoder as the engine `name`. It watches the decoder named
    by args' decoder_name on the remote daemon, qualified with
    `remote_qualifier`; with no remote daemon, it watches none. `args` has
    every arg of encoder.plugin.toml."""
    streams = {}  # producer name -> EncodeStream

    with uriel.Engine(name, max_output_size=args["max_output_size"]) as engine:
        if remote_qualifier:
            decoder = f"{remote_qualifier}.{args['decoder_name']}"
            engine.subscribe(decoder)
            logger.info("engine %s ready; watching %s", name, decoder)
        else:
            logger.info("engine %s ready; there is no remote daemon, so no decoder to watch", name)

        try:
            while True:
                engine.wait(0.1)
                while (event := engine.poll_event()) is not None:
                    _decoder_event(streams, event)
                input = engine.poll()
                if input is not None:
                    _encode_frame(engine, streams, input, args)
        finally:
            for stream in streams.values():
                stream.pipeline.stop()


class DecodeStream:
    """The H.264 stream of one producer."""

    def __init__(self, decoder):
        self.pipeline = Pipeline(
            f"appsrc name=src ! h264parse ! {decoder} ! videoconvert "
            f"! video/x-raw,format=RGB,width={FRAME_W},height={FRAME_H} ! appsink name=sink",
            "video/x-h264,stream-format=byte-stream,alignment=au",
        )
        self.expected = None  # the frame number that must come next to decode
        self.start = time.monotonic()


def decode(name, args):
    """Runs the decoder as the engine `name`. `args` has every arg of
    decoder.plugin.toml."""
    streams = {}  # producer name -> DecodeStream

    decoded_result = _result(True, False)
    dropped_result = _result(False, False)
    keyframe_needed_result = _result(False, True)

    with uriel.Engine(name, max_output_size=args["max_output_size"]) as engine:
        logger.info("engine %s ready", name)
        try:
            for input in engine:
                started = time.perf_counter()
                if input.payload_type != ENCODED_FRAME_TYPE:
                    logger.warning("discarding input %d: not an %s", input.id.seq, ENCODED_FRAME_TYPE)
                    engine.drop(result=dropped_result)
                    continue

                frame = h264_pb2.EncodedVideoFrame.FromString(bytes(input.data))
                number = frame.frame_number

                producer = input.id.producer
                stream = streams.get(producer)
                if stream is None:
                    logger.info("decoding %s", producer)
                    stream = streams[producer] = DecodeStream(args["decoder"])

                if not frame.keyframe and stream.expected != number:
                    stream.expected = None
                    if args["log_frames"]:
                        logger.info(
                            "frame %d of %s input %d follows a lost frame; waiting for a keyframe",
                            number, producer, input.id.seq,
                        )
                    engine.drop(result=keyframe_needed_result)
                    continue
                # The decoder keeps this frame as a reference whether or not
                # its output comes in time, so the next one decodes either way.
                stream.expected = number + 1

                pts_ns = int((time.monotonic() - stream.start) * 1e9)
                stream.pipeline.push(frame.data, pts_ns)
                sample, _dropped_late = stream.pipeline.pull(pts_ns, args["timeout_ms"])
                if sample is None:
                    logger.warning(
                        "frame %d of %s took over %d ms to decode; discarding it", number, producer, args["timeout_ms"]
                    )
                    engine.drop(result=dropped_result)
                    continue

                buf = sample.get_buffer()
                ok, mapinfo = buf.map(Gst.MapFlags.READ)
                try:
                    pixels = bytes(mapinfo.data)
                finally:
                    buf.unmap(mapinfo)

                if len(pixels) != RGB_FRAME_SIZE:
                    logger.warning(
                        "frame %d of %s decoded to %d bytes, not %d; discarding it",
                        number, producer, len(pixels), RGB_FRAME_SIZE,
                    )
                    engine.drop(result=dropped_result)
                    continue

                out_buf = engine.loan()
                out_buf[: len(pixels)] = pixels
                engine.replace(len(pixels), output_type="", result=decoded_result)
                if args["log_frames"]:
                    logger.info(
                        "frame %d of %s input %d keyframe=%s ms=%.2f",
                        number, producer, input.id.seq, frame.keyframe,
                        (time.perf_counter() - started) * 1e3,
                    )
        finally:
            for stream in streams.values():
                stream.pipeline.stop()
