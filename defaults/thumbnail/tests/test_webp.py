import numpy as np
import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from steeleagle_protocol.v1.messages.frame import frame_pb2
from thumbnail import main
from thumbnail.webp import FrameError, WEBP_CODEC, decode_frame, encode_frame, is_webp


def raw_frame(width=64, height=48, id=7):
    rng = np.random.default_rng(0)
    # Smooth pixels, so lossy WebP stays close to them
    x = np.linspace(0, 255, width, dtype=np.float32)
    y = np.linspace(0, 255, height, dtype=np.float32)
    pixels = np.stack(np.broadcast_arrays(x[None, :], y[:, None], (x[None, :] + y[:, None]) / 2), axis=-1)
    pixels = (pixels + rng.normal(0, 1, pixels.shape)).clip(0, 255).astype(np.uint8)
    frame = frame_pb2.RawFrame(data=pixels.tobytes(), h_res=width, v_res=height, channels=3, id=id, camera_id=2)
    frame.timestamp.CopyFrom(Timestamp(seconds=12, nanos=34))
    return frame, pixels


def test_a_frame_round_trips_with_its_fields():
    frame, pixels = raw_frame()
    encoded = frame_pb2.EncodedFrame.FromString(encode_frame(frame.SerializeToString(), quality=90, method=4))
    assert encoded.codec == WEBP_CODEC and encoded.keyframe
    assert is_webp(encoded.encoded_data)
    assert (encoded.id, encoded.camera_id, encoded.timestamp.seconds) == (7, 2, 12)
    assert len(encoded.encoded_data) < len(frame.data)

    decoded = frame_pb2.RawFrame.FromString(decode_frame(encoded.SerializeToString()))
    assert (decoded.h_res, decoded.v_res, decoded.channels, decoded.id, decoded.camera_id) == (64, 48, 3, 7, 2)
    assert decoded.timestamp == frame.timestamp
    out = np.frombuffer(decoded.data, np.uint8).reshape(48, 64, 3)
    assert np.abs(out.astype(int) - pixels).mean() < 4


def test_lossless_is_exact():
    frame, pixels = raw_frame()
    encoded = encode_frame(frame.SerializeToString(), quality=0, method=0, lossless=True)
    decoded = frame_pb2.RawFrame.FromString(decode_frame(encoded))
    assert decoded.data == pixels.tobytes()


def test_a_frame_whose_pixels_do_not_fit_is_refused():
    frame, _ = raw_frame()
    frame.h_res = 65
    with pytest.raises(FrameError):
        encode_frame(frame.SerializeToString(), quality=60, method=4)


def test_jpeg_and_bad_data_are_not_decoded():
    jpeg = frame_pb2.EncodedFrame(encoded_data=b"\xff\xd8\xff\xe0", codec=frame_pb2.CODEC_JPEG)
    with pytest.raises(FrameError):
        decode_frame(jpeg.SerializeToString())
    garbage = frame_pb2.EncodedFrame(encoded_data=b"RIFF\0\0\0\0WEBPjunk")
    with pytest.raises(FrameError):
        decode_frame(garbage.SerializeToString())


def test_every_plugin_has_defaults_and_a_runner():
    for plugin in main.DEFAULTS:
        args = main.parse_args(plugin, '{"log_frames": true}')
        assert args["log_frames"] is True and args["max_output_size"] == main.MAX_FRAME_SIZE
        assert callable(main.runner(plugin))
