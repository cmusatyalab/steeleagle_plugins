"""Tests of rescaling frames and of the resolution arg."""

import numpy as np
import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from rescale.main import ConfigError, parse_args
from rescale.rescale import RESOLUTIONS, dimensions, max_output_size, rescale
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.telemetry import telemetry_pb2


def raw_frame(width, height, channels=3, value=200):
    return frame_pb2.RawFrame(
        timestamp=Timestamp(seconds=12, nanos=34),
        data=np.full((height, width, channels), value, dtype=np.uint8).tobytes(),
        h_res=width,
        v_res=height,
        channels=channels,
        id=7,
        position_info=telemetry_pb2.PositionInfo(),
    )


@pytest.mark.parametrize("resolution", list(RESOLUTIONS))
def test_frames_are_rescaled_to_the_resolution(resolution):
    width, height = dimensions(resolution)
    data = raw_frame(1280, 720).SerializeToString()
    out = frame_pb2.RawFrame.FromString(rescale(data, (width, height)) or data)
    assert (out.h_res, out.v_res, out.channels) == (width, height, 3)
    assert len(out.data) == width * height * 3
    assert set(out.data) == {200}


def test_the_other_fields_are_kept():
    frame = raw_frame(64, 36)
    out = frame_pb2.RawFrame.FromString(rescale(frame.SerializeToString(), (32, 18)))
    assert out.id == frame.id
    assert out.timestamp == frame.timestamp
    assert out.HasField("position_info")


def test_a_frame_at_the_resolution_is_left_alone():
    assert rescale(raw_frame(854, 480).SerializeToString(), (854, 480)) is None


def test_single_channel_frames_are_rescaled():
    out = frame_pb2.RawFrame.FromString(rescale(raw_frame(64, 36, channels=1).SerializeToString(), (32, 18)))
    assert (out.h_res, out.v_res, out.channels, len(out.data)) == (32, 18, 1, 32 * 18)


def test_pixels_that_dont_fit_the_resolution_are_refused():
    frame = raw_frame(64, 36)
    frame.h_res = 65
    with pytest.raises(ValueError):
        rescale(frame.SerializeToString(), (32, 18))


SIXTEEN_NINE = {
    "4k": (3840, 2160), "1080p": (1920, 1080), "720p": (1280, 720), "480p": (854, 480),
    "360p": (640, 360), "240p": (426, 240), "180p": (320, 180),
}


def test_the_accepted_resolutions():
    assert list(RESOLUTIONS) == list(SIXTEEN_NINE)


@pytest.mark.parametrize("resolution", list(RESOLUTIONS))
def test_resolutions_default_to_16_9(resolution):
    assert dimensions(resolution) == SIXTEEN_NINE[resolution]
    assert parse_args(f'{{"resolution": "{resolution}"}}')["dimensions"] == SIXTEEN_NINE[resolution]


@pytest.mark.parametrize("aspect_ratio,expected", [
    ("16:9", (854, 480)), ("4:3", (640, 480)), ("1:1", (480, 480)),
    ("9:16", (270, 480)), ("21:9", (1120, 480)), (" 3 : 2 ", (720, 480)),
])
def test_the_aspect_ratio_sets_the_width(aspect_ratio, expected):
    assert parse_args(f'{{"resolution": "480p", "aspect_ratio": "{aspect_ratio}"}}')["dimensions"] == expected


@pytest.mark.parametrize("resolution", list(RESOLUTIONS))
@pytest.mark.parametrize("aspect_ratio", ["16:9", "4:3", "1:1", "9:16", "21:9"])
def test_dimensions_are_even_and_fit_the_output(resolution, aspect_ratio):
    width, height = dimensions(resolution, aspect_ratio)
    assert width % 2 == 0 and height % 2 == 0
    assert len(raw_frame(width, height).SerializeToString()) <= max_output_size((width, height))


@pytest.mark.parametrize("content", ['{"resolution": "720p"}', '{"resolution": "720p", "aspect_ratio": ""}',
                                     '{"resolution": "720p", "aspect_ratio": null}'])
def test_an_unset_aspect_ratio_is_16_9(content):
    assert parse_args(content)["dimensions"] == (1280, 720)


@pytest.mark.parametrize("aspect_ratio", ['"16x9"', '"16:0"', '"0:9"', '"1.5:1"', '"-4:3"', '"16:9:1"', "1.78"])
def test_bad_aspect_ratios_are_refused(aspect_ratio):
    with pytest.raises(ConfigError, match="aspect_ratio"):
        parse_args(f'{{"resolution": "720p", "aspect_ratio": {aspect_ratio}}}')


@pytest.mark.parametrize("content", [None, "", "{}", '{"resolution": ""}', '{"resolution": null}'])
def test_the_resolution_must_be_set(content):
    with pytest.raises(ConfigError, match="must be set"):
        parse_args(content)


@pytest.mark.parametrize("resolution", ['"1440p"', '"small"', "480", '["480p"]'])
def test_other_resolutions_are_refused(resolution):
    with pytest.raises(ConfigError):
        parse_args(f'{{"resolution": {resolution}}}')
