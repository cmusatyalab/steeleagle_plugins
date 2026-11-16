import numpy as np
import pytest
from conftest import make_frame, make_result
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.result import result_pb2

from detection_crop.crop import (
    FrameError,
    best_detection,
    crop,
    parse_frame,
    parse_result,
    pixel_box,
)


def box(y_min, x_min, y_max, x_max):
    return result_pb2.BoundingBox(y_min=y_min, x_min=x_min, y_max=y_max, x_max=x_max)


def test_best_detection_is_the_highest_scoring_of_the_class_ignoring_case():
    result = make_result((0, 0, 1, 1), class_name="Person", score=0.5)
    result.MergeFrom(make_result((0, 0, 0.5, 0.5), class_name="person ", score=0.8))
    result.MergeFrom(make_result((0, 0, 0.2, 0.2), class_name="car", score=0.99))
    assert best_detection(result, "person").score == pytest.approx(0.8)
    assert best_detection(result, "dog") is None
    assert best_detection(result_pb2.DetectionResult(), "person") is None


def test_pixel_box_grows_to_whole_pixels_and_clips_to_the_frame():
    assert pixel_box(box(0.25, 0.25, 0.75, 0.75), 100, 40) == (25, 10, 75, 30)
    assert pixel_box(box(0.101, 0.101, 0.499, 0.499), 10, 10) == (1, 1, 5, 5)
    assert pixel_box(box(-0.5, -0.5, 1.5, 1.5), 10, 10) == (0, 0, 10, 10)
    # Flipped corners are put right
    assert pixel_box(box(0.75, 0.75, 0.25, 0.25), 100, 40) == (25, 10, 75, 30)
    assert pixel_box(box(1.2, 0.0, 1.5, 1.0), 10, 10) is None
    assert pixel_box(box(0.5, 0.5, 0.5, 0.5), 10, 10) is None


def test_crop_keeps_the_pixels_in_the_box_and_the_other_fields():
    frame = make_frame(64, 48, color=None)
    frame.id = 7
    frame.timestamp.seconds = 123
    parsed, pixels = parse_frame(frame.SerializeToString())
    out = frame_pb2.RawFrame.FromString(crop(parsed, pixels, box(0.25, 0.5, 0.5, 1.0)))
    assert (out.h_res, out.v_res, out.channels) == (32, 12, 3)
    assert (out.id, out.timestamp.seconds) == (7, 123)
    cropped = np.frombuffer(out.data, dtype=np.uint8).reshape(12, 32, 3)
    # Pixel (y, x) of the frame is (y, x, 0)
    assert tuple(cropped[0, 0]) == (12, 32, 0)
    assert tuple(cropped[-1, -1]) == (23, 63, 0)


def test_crop_outside_the_frame_is_none():
    parsed, pixels = parse_frame(make_frame().SerializeToString())
    assert crop(parsed, pixels, box(1.1, 1.1, 1.2, 1.2)) is None


def test_parse_frame_rejects_frames_that_are_not_rgb():
    frame = make_frame(4, 4)
    frame.data = frame.data[:-1]
    with pytest.raises(FrameError):
        parse_frame(frame.SerializeToString())
    with pytest.raises(FrameError):
        parse_frame(b"\xff\xff\xff")


def test_parse_result():
    result = make_result((0, 0, 1, 1)).SerializeToString()
    assert len(parse_result("steeleagle_protocol.v1.messages.result.DetectionResult", result).detections) == 1
    assert len(parse_result("", b"").detections) == 0
    assert parse_result("something/Else", result) is None
