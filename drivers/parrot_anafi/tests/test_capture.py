"""Tests of the conversion of decoded frames to RGB and of the scale arg."""
import numpy as np
import pytest

from parrot_anafi.capture import to_rgb
from parrot_anafi.stream import Stream, scale_dimensions

WIDTH, HEIGHT = 8, 6
# Limited-range BT.601 Y, U and V of a mid gray and of a saturated red
GRAY = (126, 128, 128)
RED = (81, 90, 240)


def padded(rows, stride):
    """`rows` as bytes, each row padded to `stride`, the padding full of
    junk that would show if it were read."""
    return b''.join(row.tobytes().ljust(stride, b'\xee') for row in rows)


def nv12(yuv, stride, plane_gap=0):
    """A flat NV12 frame of the color `yuv`, and its offsets and strides."""
    y = np.full((HEIGHT, WIDTH), yuv[0], dtype=np.uint8)
    uv = np.tile(np.array(yuv[1:], dtype=np.uint8), (HEIGHT // 2, WIDTH // 2, 1))
    luma = padded(y, stride) + b'\xee' * plane_gap
    return luma + padded(uv, stride), [0, len(luma)], [stride, stride]


def i420(yuv, stride):
    """A flat I420 frame of the color `yuv`, and its offsets and strides."""
    y = np.full((HEIGHT, WIDTH), yuv[0], dtype=np.uint8)
    u = np.full((HEIGHT // 2, WIDTH // 2), yuv[1], dtype=np.uint8)
    v = np.full((HEIGHT // 2, WIDTH // 2), yuv[2], dtype=np.uint8)
    luma, cb = padded(y, stride), padded(u, stride // 2)
    return luma + cb + padded(v, stride // 2), [0, len(luma), len(luma) + len(cb)], [stride, stride // 2, stride // 2]


@pytest.mark.parametrize('stride', [WIDTH, 16, 32])
@pytest.mark.parametrize('frame', [nv12, i420])
def test_yuv_frames_become_rgb_whatever_their_padding(frame, stride):
    data, offsets, strides = frame(GRAY, stride)
    fmt = 'NV12' if frame is nv12 else 'I420'
    rgb = to_rgb(data, fmt, WIDTH, HEIGHT, offsets, strides)
    assert rgb.shape == (HEIGHT, WIDTH, 3)
    assert np.abs(rgb.astype(int) - 128).max() <= 2


def test_a_gap_between_the_planes_is_skipped():
    data, offsets, strides = nv12(GRAY, 16, plane_gap=64)
    assert np.abs(to_rgb(data, 'NV12', WIDTH, HEIGHT, offsets, strides).astype(int) - 128).max() <= 2


@pytest.mark.parametrize('frame,fmt', [(nv12, 'NV12'), (i420, 'I420')])
def test_channels_are_in_rgb_order(frame, fmt):
    data, offsets, strides = frame(RED, 16)
    r, g, b = to_rgb(data, fmt, WIDTH, HEIGHT, offsets, strides)[0, 0]
    assert r > 230 and g < 25 and b < 25


def test_rgb_frames_are_copied_without_their_padding():
    rows = np.arange(HEIGHT * WIDTH * 3, dtype=np.uint8).reshape(HEIGHT, WIDTH, 3)
    data = padded(rows, 32)
    rgb = to_rgb(data, 'RGB', WIDTH, HEIGHT, [0], [32])
    np.testing.assert_array_equal(rgb, rows)
    assert rgb.flags.owndata


def test_other_formats_are_refused():
    with pytest.raises(ValueError):
        to_rgb(b'\0' * 1024, 'YUY2', WIDTH, HEIGHT, [0], [16])


@pytest.mark.parametrize('scale,dimensions', [
    ('480p', (854, 480)), ('360p', (640, 360)), ('240p', (426, 240)), ('180p', (320, 180)),
])
def test_scales_keep_the_aspect_ratio_with_even_dimensions(scale, dimensions):
    assert scale_dimensions(scale) == dimensions
    width, height = dimensions
    assert width % 2 == 0 and height % 2 == 0
    assert abs(width / height - 16 / 9) < 0.01


@pytest.mark.parametrize('scale', ['', None, '720p', '1080p', 'small', '480P', ' 480p', 480, ['480p']])
def test_a_blank_or_unknown_scale_leaves_frames_alone(scale):
    assert scale_dimensions(scale) is None


def test_stream_takes_its_scale():
    assert Stream(drone=None, ip='192.0.2.1', scale='240p').scale == (426, 240)
    assert Stream(drone=None, ip='192.0.2.1').scale is None
