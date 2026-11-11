"""Reading frames and detections, and cropping a frame to a detection."""

import math

import numpy as np
from google.protobuf.message import DecodeError
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.result import result_pb2

RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"
DETECTION_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/DetectionResult"
RGB_CHANNELS = 3
# Room in an output for a RawFrame's fields besides its pixels: its ids,
# timestamp and pose.
RAW_FRAME_HEADROOM = 16 << 10


class FrameError(ValueError):
    pass


def type_name(payload_type):
    """Engines write a type's name with slashes or, as detection-qnn does,
    dots."""
    return payload_type.replace(".", "/")


def max_output_size(max_width, max_height):
    """The largest crop this engine sends."""
    return max_width * max_height * RGB_CHANNELS + RAW_FRAME_HEADROOM


def parse_frame(data):
    """The serialized RGB RawFrame `data`, and its pixels as an (h, w, 3)
    array. Raises FrameError for anything else."""
    try:
        frame = frame_pb2.RawFrame.FromString(data)
    except DecodeError as err:
        raise FrameError(f"not a RawFrame: {err}") from None
    width, height = frame.h_res, frame.v_res
    channels = frame.channels or RGB_CHANNELS
    if (
        channels != RGB_CHANNELS
        or width == 0
        or height == 0
        or len(frame.data) != width * height * RGB_CHANNELS
    ):
        raise FrameError(
            f"{len(frame.data)} bytes of {channels}-channel pixels, "
            f"not a {width}x{height} RGB frame"
        )
    pixels = np.frombuffer(frame.data, dtype=np.uint8)
    return frame, pixels.reshape(height, width, RGB_CHANNELS)


def parse_result(payload_type, result):
    """The DetectionResult serialized in `result`, or None if it is not one.
    An empty result, as a detector that found nothing sends, is a
    DetectionResult with no detections."""
    if payload_type and type_name(payload_type) != DETECTION_RESULT_TYPE:
        return None
    try:
        return result_pb2.DetectionResult.FromString(result)
    except DecodeError:
        return None


def normalize_class(name):
    return name.strip().casefold()


def best_detection(result, class_name):
    """The highest-scoring detection of `class_name` (as normalize_class
    gives it) in the DetectionResult `result`, or None if it has none."""
    matches = [d for d in result.detections if normalize_class(d.class_name) == class_name]
    return max(matches, key=lambda d: d.score, default=None)


def pixel_box(bbox, width, height):
    """The normalized BoundingBox `bbox` in a `width` x `height` frame, as
    (left, top, right, bottom) pixels: grown to whole pixels and clipped to
    the frame. None if nothing of it is in the frame."""
    left = max(0, math.floor(min(bbox.x_min, bbox.x_max) * width))
    top = max(0, math.floor(min(bbox.y_min, bbox.y_max) * height))
    right = min(width, math.ceil(max(bbox.x_min, bbox.x_max) * width))
    bottom = min(height, math.ceil(max(bbox.y_min, bbox.y_max) * height))
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def crop(frame, pixels, bbox):
    """The RawFrame `frame`, whose pixels are the (h, w, 3) array `pixels`,
    cropped to the normalized BoundingBox `bbox` and serialized, with its
    other fields as they were; `frame` is cropped in place. None if the box
    is not in the frame."""
    box = pixel_box(bbox, frame.h_res, frame.v_res)
    if box is None:
        return None
    left, top, right, bottom = box
    frame.data = pixels[top:bottom, left:right].tobytes()
    frame.h_res, frame.v_res = right - left, bottom - top
    frame.channels = RGB_CHANNELS
    return frame.SerializeToString()
