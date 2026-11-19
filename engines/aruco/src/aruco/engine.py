"""The ArUco engine: RawFrame in, DetectionResult out, a detection of class
aruco_<id> per marker found."""

import logging

import cv2
import numpy as np
import uriel
from google.protobuf.message import DecodeError
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.result import result_pb2

logger = logging.getLogger("aruco")

RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"
DETECTION_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/DetectionResult"
RGB_CHANNELS = 3
# The class name of marker 7 is aruco_7, as the old Gabriel engine named it.
CLASS_PREFIX = "aruco_"
# A marker is decoded or not, so it has no confidence to report.
MARKER_SCORE = 1.0
# How long the engine waits for an input before it checks whether to stop.
POLL_INTERVAL_S = 0.1


class FrameError(Exception):
    pass


def make_detector(dictionary):
    """An ArucoDetector for the named predefined dictionary, e.g.
    "DICT_ARUCO_ORIGINAL"."""
    markers = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dictionary))
    return cv2.aruco.ArucoDetector(markers, cv2.aruco.DetectorParameters())


def decode_gray(frame):
    """The frame's pixels as a grayscale array of shape (height, width)."""
    width, height = frame.h_res, frame.v_res
    if (
        frame.channels != RGB_CHANNELS
        or len(frame.data) != width * height * RGB_CHANNELS
    ):
        raise FrameError(
            f"{len(frame.data)} bytes is not a {width}x{height} frame of {frame.channels} channels"
        )
    rgb = np.frombuffer(frame.data, dtype=np.uint8).reshape(height, width, RGB_CHANNELS)
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)


def detect(frame, detector, only_ids=()):
    """The DetectionResult for a RawFrame: a box around each marker
    `detector` finds, of the given ids (all if empty)."""
    gray = decode_gray(frame)
    height, width = gray.shape
    corners, ids, _ = detector.detectMarkers(gray)
    result = result_pb2.DetectionResult()
    if ids is None:
        return result
    wanted = set(only_ids)
    # ids is (N, 1) in some OpenCV versions and (N,) in others
    for marker, marker_id in zip(corners, np.asarray(ids).reshape(-1)):
        if wanted and int(marker_id) not in wanted:
            continue
        # The marker's four corners, as (x, y) pixels
        points = marker.reshape(-1, 2)
        x_min, y_min = points.min(axis=0)
        x_max, y_max = points.max(axis=0)
        result.detections.add(
            class_name=f"{CLASS_PREFIX}{int(marker_id)}",
            score=MARKER_SCORE,
            bbox=result_pb2.BoundingBox(
                x_min=_clip(x_min / width),
                y_min=_clip(y_min / height),
                x_max=_clip(x_max / width),
                y_max=_clip(y_max / height),
            ),
        )
    return result


def _clip(fraction):
    # Corners are refined to subpixels, so one can fall just outside the frame
    return min(max(float(fraction), 0.0), 1.0)


def _frame_result(input, detector, only_ids):
    """The serialized result for an input; empty if it cannot be read, so the
    engine keeps serving the frames after it."""
    try:
        if input.payload_type != RAW_FRAME_TYPE:
            raise FrameError(
                f"payload is a {input.payload_type!r}, not a {RAW_FRAME_TYPE}"
            )
        frame = frame_pb2.RawFrame.FromString(bytes(input.data))
        return detect(frame, detector, only_ids).SerializeToString()
    except (FrameError, DecodeError) as err:
        logger.warning("input %d: %s", input.id.seq, err)
    except Exception:
        logger.exception("input %d: marker detection failed", input.id.seq)
    return result_pb2.DetectionResult().SerializeToString()


def run(
    name,
    detector,
    only_ids,
    *,
    stop,
    max_output_size,
    early_discard=False,
    on_ready=None,
):
    """Serves the engine `name` until the threading.Event `stop` is set. Each
    frame is forwarded on, with its markers as the result; with
    `early_discard`, a frame with no markers is dropped instead."""
    with uriel.Engine(
        name, result_type=DETECTION_RESULT_TYPE, max_output_size=max_output_size
    ) as engine:
        logger.info("engine %s ready", name)
        if on_ready is not None:
            on_ready()
        while not stop.is_set():
            input = engine.next(POLL_INTERVAL_S)
            if input is None:
                continue
            result = _frame_result(input, detector, only_ids)
            # A DetectionResult with no detections serializes to no bytes
            if early_discard and not result:
                engine.drop()
            else:
                engine.forward(result=result)
