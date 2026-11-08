"""Uriel engines that carry frames across the network as WebP: the encoder
replaces each RGB RawFrame with an EncodedFrame holding it as WebP, and the
decoder, on the remote side, replaces each such EncodedFrame with the RGB
RawFrame it decodes to.

WebP is about 40% smaller than JPEG at equal PSNR, which suits small images
sent over a thin link, such as the tiles cliped picks out of a frame.

The protocol's Codec enum has no WebP value yet, so a WebP frame is sent as
CODEC_UNSPECIFIED, and the decoder takes a frame for WebP by its RIFF/WEBP
header. Like JPEG, every WebP frame stands alone and is a keyframe.

Unlike the JPEG encoder, which numbers the frames of each producer, the WebP
encoder keeps each frame's own id, so a tile keeps the id of the frame it was
cut from.
"""
import io
import logging
import time

import uriel
from PIL import Image

import steeleagle_protocol.v1.messages.frame.frame_pb2 as frame_proto

from thumbnail.frames import ENCODED_FRAME_TYPE, RAW_FRAME_TYPE, RGB_CHANNELS, copy_fields, same_type

logger = logging.getLogger("thumbnail.webp")

# The codec WebP frames are sent as, until the protocol has CODEC_WEBP.
WEBP_CODEC = getattr(frame_proto, "CODEC_WEBP", frame_proto.CODEC_UNSPECIFIED)


class FrameError(ValueError):
    """An input that is not a frame the engine can encode or decode."""


def is_webp(data):
    """Whether `data` starts with a WebP file's RIFF header."""
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def encode_frame(data, *, quality, method, lossless=False):
    """The serialized RGB RawFrame `data` as a serialized EncodedFrame of WebP,
    with the frame's ids, timestamp and pose. Raises FrameError if it is not
    an RGB RawFrame whose pixels fit its resolution."""
    frame = frame_proto.RawFrame.FromString(data)
    width, height = frame.h_res, frame.v_res
    if frame.channels != RGB_CHANNELS or width == 0 or len(frame.data) != width * height * RGB_CHANNELS:
        raise FrameError(f"{len(frame.data)} bytes, not a {width}x{height} RGB frame")

    image = Image.frombytes("RGB", (width, height), frame.data)
    buf = io.BytesIO()
    image.save(buf, format="WEBP", quality=quality, method=method, lossless=lossless)
    out = frame_proto.EncodedFrame(
        encoded_data=buf.getvalue(),
        id=frame.id,
        camera_id=frame.camera_id,
        codec=WEBP_CODEC,
        keyframe=True,
    )
    copy_fields(frame, out)
    return out.SerializeToString()


def decode_frame(data):
    """The serialized EncodedFrame of WebP `data` as a serialized RGB
    RawFrame, with the frame's ids, timestamp and pose. Raises FrameError if
    it does not hold a WebP image."""
    frame = frame_proto.EncodedFrame.FromString(data)
    if frame.codec not in (WEBP_CODEC, frame_proto.CODEC_UNSPECIFIED) or not is_webp(frame.encoded_data):
        raise FrameError(f"not a WebP frame (codec {frame.codec})")
    try:
        with Image.open(io.BytesIO(frame.encoded_data), formats=["WEBP"]) as image:
            rgb = image.convert("RGB")
    except (OSError, ValueError) as err:
        raise FrameError(f"bad WebP image: {err}") from None
    out = frame_proto.RawFrame(
        data=rgb.tobytes(),
        h_res=rgb.width,
        v_res=rgb.height,
        channels=RGB_CHANNELS,
        id=frame.id,
        camera_id=frame.camera_id,
    )
    copy_fields(frame, out)
    return out.SerializeToString()


def _run(name, args, input_type, output_type, convert, verb):
    """Serves the engine `name`, replacing each input of `input_type` with
    `convert(data)`, of `output_type`."""
    limit = args["max_output_size"]
    with uriel.Engine(name, max_output_size=limit) as engine:
        logger.info("engine %s ready", name)
        for input in engine:
            started = time.perf_counter()
            if not same_type(input.payload_type, input_type):
                logger.warning("discarding input %d: not a %s", input.id.seq, input_type)
                engine.drop()
                continue
            try:
                payload = convert(bytes(input.data))
            except (FrameError, ValueError) as err:
                # ValueError also covers a protobuf DecodeError
                logger.warning("discarding input %d: %s", input.id.seq, err)
                engine.drop()
                continue
            if len(payload) > limit:
                logger.warning(
                    "input %d %s to %d bytes, over max_output_size; discarding it", input.id.seq, verb, len(payload)
                )
                engine.drop()
                continue
            engine.loan()[: len(payload)] = payload
            engine.replace(len(payload), output_type=output_type)
            if args["log_frames"]:
                logger.info(
                    "input %d of %s %s bytes=%d ms=%.2f",
                    input.id.seq,
                    input.id.producer,
                    verb,
                    len(payload),
                    (time.perf_counter() - started) * 1e3,
                )


def encode(name, args):
    """Runs the encoder as the engine `name`. `args` has every arg of
    webp_encoder.plugin.toml."""
    options = {"quality": args["quality"], "method": args["method"], "lossless": args["lossless"]}
    _run(name, args, RAW_FRAME_TYPE, ENCODED_FRAME_TYPE, lambda data: encode_frame(data, **options), "encoded")


def decode(name, args):
    """Runs the decoder as the engine `name`. `args` has every arg of
    webp_decoder.plugin.toml."""
    _run(name, args, ENCODED_FRAME_TYPE, RAW_FRAME_TYPE, decode_frame, "decoded")
