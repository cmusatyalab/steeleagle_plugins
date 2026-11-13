"""Tests of the vehicle-side frame work: the preprocess engine, the keyframe
tracker ported from the SwiftMap server, and reachback's keyframe filter."""

import io
from dataclasses import dataclass

import cv2
import numpy as np
import pytest
from conftest import position
from PIL import Image

from steeleagle_protocol.v1.common import common_pb2
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.telemetry import telemetry_pb2
from swiftmap.onboard import (
    NO_FIX, NOT_KEYFRAME, TOO_CLOSE, FrameTracker, KeyframeFilter, crop_box, encode_frame,
    haversine_m, model_size, preprocess, to_gray,
)
from swiftmap.protocol import ENCODED_FRAME_TYPE, FrameError, is_webp, webp_bytes

WIDTH, HEIGHT = 320, 240
# A blurred-noise scene, larger than a frame; each frame is a crop of it, so
# moving the crop is moving the camera.
_rng = np.random.default_rng(0)
SCENE = cv2.normalize(
    cv2.GaussianBlur((_rng.random((400, 640)) * 255).astype(np.uint8), (0, 0), 2),
    None, 0, 255, cv2.NORM_MINMAX,
)
FLAT = np.full((HEIGHT, WIDTH), 128, dtype=np.uint8)


def view(x, y=0):
    """The grayscale frame of the camera moved x px right and y px down."""
    return np.ascontiguousarray(SCENE[y:y + HEIGHT, x:x + WIDTH])


def keyframes(tracker, frames):
    return [tracker.is_keyframe(f) for f in frames]


def test_a_frame_is_a_keyframe_once_it_moved_min_disparity_from_the_last_one():
    # Disparity is about half a sideways move (see the next test), so 6 px is
    # ~3 and 30 px ~12-15
    tracker = FrameTracker(min_disparity=10)
    assert keyframes(tracker, [view(0), view(6), view(30), view(36), view(60)]) == [
        True, False, True, False, True,
    ]


def test_disparity_is_the_servers_mean_of_dx_and_dy():
    tracker = FrameTracker(min_disparity=10)
    tracker.is_keyframe(view(0))
    tracker.is_keyframe(view(6))
    # 6 px sideways: |dx| = 6 and |dy| = 0, so 3, where Euclidean would be 6
    assert tracker.last_disparity == pytest.approx(3.0, abs=0.5)


def test_a_featureless_frame_is_dropped_and_the_next_frame_starts_over():
    tracker = FrameTracker(min_disparity=10)
    assert keyframes(tracker, [FLAT, FLAT, view(0), view(2)]) == [False, False, True, False]


def test_without_min_disparity_every_frame_is_kept():
    assert keyframes(FrameTracker(min_disparity=0), [view(0), view(0), FLAT]) == [True] * 3


def test_a_new_resolution_starts_over():
    tracker = FrameTracker(min_disparity=10)
    tracker.is_keyframe(view(0))
    assert tracker.is_keyframe(cv2.resize(view(0), (WIDTH // 2, HEIGHT // 2)))


@dataclass
class Id:
    seq: int
    producer: str = "drone.rgb"
    session: int = 1


@dataclass
class Input:
    payload_type: str
    data: bytes
    id: Id


def webp_frame(gray, id=0, gps=None):
    """A lossless WebP EncodedFrame of `gray`, as the preprocess engine sends one."""
    webp = cv2.imencode(".webp", cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR),
                        [cv2.IMWRITE_WEBP_QUALITY, 101])[1].tobytes()
    frame = frame_pb2.EncodedFrame(encoded_data=webp, id=id, codec=frame_pb2.CODEC_UNSPECIFIED)
    if gps is not None:
        frame.position_info.CopyFrom(telemetry_pb2.PositionInfo(
            global_position=common_pb2.GlobalPosition(latitude=gps[0], longitude=gps[1], altitude=10.0)
        ))
    return frame.SerializeToString()


def webp_input(gray, seq=0, gps=None, **id):
    return Input(ENCODED_FRAME_TYPE, webp_frame(gray, seq, gps), Id(seq, **id))


# Points ~1.1 m, ~11.1 m and ~12.2 m north of HOME
HOME, NEAR, FAR, FARTHER = (40.0, -79.0), (40.00001, -79.0), (40.0001, -79.0), (40.00011, -79.0)


def test_haversine():
    assert haversine_m(HOME, HOME) == 0
    assert haversine_m(HOME, FAR) == pytest.approx(11.12, abs=0.01)


def test_frames_within_send_distance_of_the_last_one_forwarded_are_dropped():
    f = KeyframeFilter(min_disparity=0, send_distance=5.0)
    kept = [f.keep(webp_input(view(0), seq, gps)) for seq, gps in enumerate([HOME, NEAR, FAR], 1)]
    assert kept == [True, False, True]
    assert f.dropped == {TOO_CLOSE: 1}


def test_frames_without_a_fix_are_dropped_unless_send_distance_is_0():
    f = KeyframeFilter(min_disparity=0, send_distance=5.0)
    assert not f.keep(webp_input(view(0), 1))
    assert f.dropped == {NO_FIX: 1}
    assert KeyframeFilter(min_disparity=0, send_distance=0).keep(webp_input(view(0), 1))


def test_a_frame_too_close_is_dropped_before_the_tracker_sees_it():
    f = KeyframeFilter(min_disparity=10, send_distance=5.0)
    assert f.keep(webp_input(view(0), 1, HOME))
    # Would be a keyframe, but is too close: dropped, so it is not the keyframe
    assert not f.keep(webp_input(view(30), 2, NEAR))
    # Compared with view(0), the last frame forwarded, not view(30) 6 px away
    assert f.keep(webp_input(view(36), 3, FAR))
    assert f.dropped == {TOO_CLOSE: 1}


def test_a_frame_far_enough_but_not_a_keyframe_is_dropped_and_the_distance_still_counts_from_the_last_one_forwarded():
    f = KeyframeFilter(min_disparity=10, send_distance=5.0)
    assert f.keep(webp_input(view(0), 1, HOME))
    assert not f.keep(webp_input(view(2), 2, FAR))
    # ~1.1 m from FAR, but ~12.2 m from HOME, the last frame forwarded
    assert f.keep(webp_input(view(36), 3, FARTHER))
    assert f.dropped == {NOT_KEYFRAME: 1}


def test_each_producer_and_session_is_tracked_on_its_own():
    f = KeyframeFilter(min_disparity=10, send_distance=5.0)
    assert f.keep(webp_input(view(0), 1, HOME, producer="a.rgb"))
    assert not f.keep(webp_input(view(2), 2, FAR, producer="a.rgb"))
    # Another camera's first frame is its first keyframe, wherever it is
    assert f.keep(webp_input(view(2), 1, HOME, producer="b.rgb"))
    # So is a's first frame of a new session
    assert f.keep(webp_input(view(2), 1, HOME, producer="a.rgb", session=2))
    assert (f.received, f.kept) == (4, 3)
    assert f.dropped == {NOT_KEYFRAME: 1}


def test_lossless_webp_frames_are_read_in_grayscale_exactly():
    gray = view(0)
    assert np.array_equal(to_gray(frame_pb2.EncodedFrame.FromString(webp_frame(gray))), gray)


def test_jpeg_h264_raw_frames_and_other_payloads_are_not_read():
    f = KeyframeFilter(min_disparity=10, send_distance=0)
    jpeg = cv2.imencode(".jpg", view(0))[1].tobytes()
    for frame in (frame_pb2.EncodedFrame(encoded_data=jpeg, codec=frame_pb2.CODEC_JPEG),
                  frame_pb2.EncodedFrame(encoded_data=b"\0\0\1", codec=frame_pb2.CODEC_H264)):
        with pytest.raises(FrameError):
            f.keep(Input(ENCODED_FRAME_TYPE, frame.SerializeToString(), Id(1)))
    with pytest.raises(FrameError):
        f.keep(Input("steeleagle_protocol/v1/messages/frame/RawFrame", b"", Id(2)))
    with pytest.raises(FrameError):
        f.keep(Input("steeleagle_protocol/v1/messages/telemetry/Telemetry", b"", Id(3)))


def test_payload_types_may_be_written_with_dots():
    f = KeyframeFilter(min_disparity=10, send_distance=0)
    assert f.keep(Input(ENCODED_FRAME_TYPE.replace("/", "."), webp_frame(view(0)), Id(1)))


ARGS = dict(image_resolution=512, patch_size=16, lossless=True, quality=90)


def raw_frame(rgb, id=3):
    frame = frame_pb2.RawFrame(data=rgb.tobytes(), h_res=rgb.shape[1], v_res=rgb.shape[0],
                               channels=3, id=id, camera_id=2)
    frame.position_info.CopyFrom(position(40.0, -79.0, 12.0))
    return frame.SerializeToString()


@pytest.mark.parametrize("size, expected", [
    ((1920, 1080), (688, 384)),
    ((1280, 720), (688, 384)),
    ((1024, 1024), (512, 512)),
    ((1080, 1920), (384, 688)),
])
def test_the_model_size_is_whole_patches_about_512_squared(size, expected):
    assert model_size(*size, 512, 16) == expected


def test_only_aspect_ratios_outside_half_to_two_are_cropped():
    assert crop_box(1920, 1080) == (0, 0, 1920, 1080)
    assert crop_box(3000, 1000) == (500, 0, 2500, 1000)
    assert crop_box(1000, 3000) == (0, 500, 1000, 2500)


def test_preprocess_is_a_bicubic_resize_to_the_model_size():
    image = Image.fromarray(np.random.default_rng(0).integers(0, 255, (1080, 1920, 3), np.uint8))
    expected = image.resize((688, 384), Image.Resampling.BICUBIC)
    assert np.array_equal(np.asarray(preprocess(image, 512, 16)), np.asarray(expected))


def test_a_frame_goes_out_as_lossless_webp_at_the_model_size_with_its_id_and_pose():
    rgb = np.random.default_rng(1).integers(0, 255, (1080, 1920, 3), np.uint8)
    out = frame_pb2.EncodedFrame.FromString(encode_frame(raw_frame(rgb), **ARGS))
    assert (out.id, out.camera_id, out.codec) == (3, 2, frame_pb2.CODEC_UNSPECIFIED)
    assert out.position_info.global_position.latitude == 40.0
    assert is_webp(webp_bytes(out))
    sent = np.asarray(Image.open(io.BytesIO(out.encoded_data)).convert("RGB"))
    expected = Image.fromarray(rgb).resize((688, 384), Image.Resampling.BICUBIC)
    assert np.array_equal(sent, np.asarray(expected))
    assert to_gray(out).shape == (384, 688)


def test_a_frame_whose_pixels_do_not_fit_its_size_is_refused():
    frame = frame_pb2.RawFrame(data=b"\0" * 10, h_res=4, v_res=4, channels=3)
    with pytest.raises(FrameError):
        encode_frame(frame.SerializeToString(), **ARGS)


def test_preprocess_args_default_from_its_plugin_toml_and_are_checked():
    from swiftmap.main import PREPROCESS_PLUGIN, ConfigError, parse_args
    args = parse_args(PREPROCESS_PLUGIN, "")
    assert args == {"image_resolution": 512, "patch_size": 16, "lossless": True, "quality": 90,
                    "max_output_size": 6237184}
    for bad in ('{"image_resolution": 500}', '{"quality": 101}', '{"patch_size": 0}'):
        with pytest.raises(ConfigError):
            parse_args(PREPROCESS_PLUGIN, bad)
