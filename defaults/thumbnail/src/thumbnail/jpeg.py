"""Uriel engines that carry a vehicle's camera frames across the network as
JPEG: the encoder replaces each RawFrame a driver publishes with an
EncodedFrame (CODEC_JPEG), and the decoder, on the remote side, replaces each
EncodedFrame with the RGB RawFrame it decodes to.

Every JPEG frame stands alone, so unlike H.264 a frame Uriel skips or times
out on the way to the decoder costs only that frame, and the encoder need not
watch the decoder.

Each producer (camera) gets a pipeline of its own. The encoder numbers the
frames of a producer in EncodedFrame.id.
"""
import logging
import time

import uriel

import steeleagle_protocol.v1.messages.frame.frame_pb2 as frame_proto

from thumbnail.frames import ENCODED_FRAME_TYPE, RAW_FRAME_TYPE, RGB_CHANNELS, copy_fields
from thumbnail.gst import Gst, Pipeline, pad_rows, unpad_rows

logger = logging.getLogger("thumbnail.jpeg")


class EncodeStream:
    """The JPEG pipeline of one producer, at one resolution."""

    def __init__(self, encoder, width, height):
        self.width = width
        self.height = height
        self.pipeline = Pipeline(
            f"appsrc name=src ! videoconvert ! {encoder} ! image/jpeg ! appsink name=sink",
            f"video/x-raw,format=RGB,width={width},height={height},framerate=0/1",
        )
        self.next_number = 0
        self.start = time.monotonic()


def _encode_frame(engine, streams, input, args):
    started = time.perf_counter()
    if input.payload_type != RAW_FRAME_TYPE:
        logger.warning("discarding input %d: not a %s", input.id.seq, RAW_FRAME_TYPE)
        engine.drop()
        return

    frame = frame_proto.RawFrame.FromString(bytes(input.data))
    width, height = frame.h_res, frame.v_res
    if frame.channels != RGB_CHANNELS or len(frame.data) != width * height * RGB_CHANNELS:
        logger.warning(
            "discarding input %d: %d bytes, not a %dx%d RGB frame", input.id.seq, len(frame.data), width, height
        )
        engine.drop()
        return

    producer = input.id.producer
    stream = streams.get(producer)
    if stream is None or (stream.width, stream.height) != (width, height):
        if stream is not None:
            stream.pipeline.stop()
        logger.info("encoding %s at %dx%d", producer, width, height)
        stream = streams[producer] = EncodeStream(args["encoder"], width, height)
    pipeline = stream.pipeline

    pts_ns = int((time.monotonic() - stream.start) * 1e9)
    pipeline.push(pad_rows(frame.data, width, height), pts_ns)
    sample = pipeline.pull(pts_ns, args["timeout_ms"])

    number = stream.next_number
    if sample is None:
        logger.warning("frame %d of %s took over %d ms to encode; discarding it", number, producer, args["timeout_ms"])
        engine.drop()
        return

    buf = sample.get_buffer()
    ok, mapinfo = buf.map(Gst.MapFlags.READ)
    try:
        encoded_bytes = bytes(mapinfo.data)
    finally:
        buf.unmap(mapinfo)

    out = frame_proto.EncodedFrame(
        encoded_data=encoded_bytes,
        id=number,
        camera_id=frame.camera_id,
        codec=frame_proto.CODEC_JPEG,
        keyframe=True,
    )
    copy_fields(frame, out)
    payload = out.SerializeToString()
    if len(payload) > args["max_output_size"]:
        logger.warning("frame %d of %s is %d bytes, over max_output_size; discarding it", number, producer, len(payload))
        engine.drop()
        return

    out_buf = engine.loan()
    out_buf[: len(payload)] = payload
    engine.replace(len(payload), output_type=ENCODED_FRAME_TYPE)
    stream.next_number += 1
    if args["log_frames"]:
        logger.info(
            "frame %d of %s input %d bytes=%d ms=%.2f",
            number,
            producer,
            input.id.seq,
            len(payload),
            (time.perf_counter() - started) * 1e3,
        )


def encode(name, args):
    """Runs the encoder as the engine `name`. `args` has every arg of
    jpeg_encoder.plugin.toml."""
    streams = {}  # producer name -> EncodeStream

    with uriel.Engine(name, max_output_size=args["max_output_size"]) as engine:
        logger.info("engine %s ready", name)
        try:
            for input in engine:
                _encode_frame(engine, streams, input, args)
        finally:
            for stream in streams.values():
                stream.pipeline.stop()


class DecodeStream:
    """The JPEG pipeline of one producer."""

    def __init__(self, decoder):
        self.pipeline = Pipeline(
            f"appsrc name=src ! {decoder} ! videoconvert ! video/x-raw,format=RGB ! appsink name=sink",
            "image/jpeg",
        )
        self.start = time.monotonic()


def _decode_frame(engine, streams, input, args):
    started = time.perf_counter()
    if input.payload_type != ENCODED_FRAME_TYPE:
        logger.warning("discarding input %d: not an %s", input.id.seq, ENCODED_FRAME_TYPE)
        engine.drop()
        return

    frame = frame_proto.EncodedFrame.FromString(bytes(input.data))
    if frame.codec != frame_proto.CODEC_JPEG:
        logger.warning("discarding input %d: not JPEG", input.id.seq)
        engine.drop()
        return
    number = frame.id

    producer = input.id.producer
    stream = streams.get(producer)
    if stream is None:
        logger.info("decoding %s", producer)
        stream = streams[producer] = DecodeStream(args["decoder"])

    pts_ns = int((time.monotonic() - stream.start) * 1e9)
    stream.pipeline.push(frame.encoded_data, pts_ns)
    sample = stream.pipeline.pull(pts_ns, args["timeout_ms"])
    if sample is None:
        logger.warning("frame %d of %s took over %d ms to decode; discarding it", number, producer, args["timeout_ms"])
        engine.drop()
        return

    caps = sample.get_caps().get_structure(0)
    _, width = caps.get_int("width")
    _, height = caps.get_int("height")
    buf = sample.get_buffer()
    ok, mapinfo = buf.map(Gst.MapFlags.READ)
    try:
        pixels = unpad_rows(bytes(mapinfo.data), width, height)
    finally:
        buf.unmap(mapinfo)

    out = frame_proto.RawFrame(
        data=pixels,
        h_res=width,
        v_res=height,
        channels=RGB_CHANNELS,
        id=number,
        camera_id=frame.camera_id,
    )
    copy_fields(frame, out)
    payload = out.SerializeToString()
    if len(payload) > args["max_output_size"]:
        logger.warning(
            "frame %d of %s decoded to %d bytes, over max_output_size; discarding it", number, producer, len(payload)
        )
        engine.drop()
        return

    out_buf = engine.loan()
    out_buf[: len(payload)] = payload
    engine.replace(len(payload), output_type=RAW_FRAME_TYPE)
    if args["log_frames"]:
        logger.info(
            "frame %d of %s input %d ms=%.2f",
            number,
            producer,
            input.id.seq,
            (time.perf_counter() - started) * 1e3,
        )


def decode(name, args):
    """Runs the decoder as the engine `name`. `args` has every arg of
    jpeg_decoder.plugin.toml."""
    streams = {}  # producer name -> DecodeStream

    with uriel.Engine(name, max_output_size=args["max_output_size"]) as engine:
        logger.info("engine %s ready", name)
        try:
            for input in engine:
                _decode_frame(engine, streams, input, args)
        finally:
            for stream in streams.values():
                stream.pipeline.stop()
