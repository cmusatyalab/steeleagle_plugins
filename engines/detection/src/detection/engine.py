"""The detection engine: RawFrame in, DetectionResult out."""

import logging

import numpy as np
import uriel
from google.protobuf.message import DecodeError

from detection.geolocate import estimate_location
from detection.predictors import keep_classes
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.result import result_pb2

logger = logging.getLogger("detection")

RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"
DETECTION_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/DetectionResult"
RGB_CHANNELS = 3
# How long the engine waits for an input before it checks whether to stop.
POLL_INTERVAL_S = 0.1


class FrameError(Exception):
    pass


def decode_image(frame):
    """The frame's pixels as an RGB array of shape (height, width, 3)."""
    width, height = frame.h_res, frame.v_res
    if (
        frame.channels != RGB_CHANNELS
        or len(frame.data) != width * height * RGB_CHANNELS
    ):
        raise FrameError(
            f"{len(frame.data)} bytes is not a {width}x{height} frame of {frame.channels} channels"
        )
    pixels = np.frombuffer(frame.data, dtype=np.uint8)
    # frombuffer is read-only, which PyTorch warns about
    return pixels.reshape(height, width, RGB_CHANNELS).copy()


def detect(frame, predictor, classes, geofence, camera):
    """The DetectionResult for a RawFrame: what `predictor` finds, of the
    named `classes` (all if empty) and, if there is a `geofence`, estimated to
    be inside it."""
    detected = keep_classes(predictor.predict(decode_image(frame)), classes)
    result = result_pb2.DetectionResult()
    for d in detected:
        if geofence is not None:
            location = estimate_location(
                d.box, frame.position_info, frame.gimbal_info, camera
            )
            if location is None or not geofence.contains(*location):
                continue
        y_min, x_min, y_max, x_max = d.box
        result.detections.add(
            class_name=d.class_name,
            score=d.score,
            bbox=result_pb2.BoundingBox(
                y_min=y_min, x_min=x_min, y_max=y_max, x_max=x_max
            ),
        )
    return result


def _frame_result(input, predictor, classes, geofence, camera):
    """The serialized result for an input; empty if it cannot be read, so the
    engine keeps serving the frames after it."""
    try:
        if input.payload_type != RAW_FRAME_TYPE:
            raise FrameError(
                f"payload is a {input.payload_type!r}, not a {RAW_FRAME_TYPE}"
            )
        frame = frame_pb2.RawFrame.FromString(bytes(input.data))
        return detect(frame, predictor, classes, geofence, camera).SerializeToString()
    except (FrameError, DecodeError) as err:
        logger.warning("input %d: %s", input.id.seq, err)
    except Exception:
        logger.exception("input %d: detection failed", input.id.seq)
    return result_pb2.DetectionResult().SerializeToString()


def run(
    name, predictor, classes, geofence, camera, *, stop, max_output_size, on_ready=None
):
    """Serves the engine `name` until the threading.Event `stop` is set. Each
    frame is forwarded on, with its detections as the result."""
    with uriel.Engine(
        name, result_type=DETECTION_RESULT_TYPE, max_output_size=max_output_size
    ) as engine:
        logger.info("engine %s ready", name)
        if on_ready is not None:
            on_ready()
        while not stop.is_set():
            input = engine.next(POLL_INTERVAL_S)
            if input is not None:
                engine.forward(
                    result=_frame_result(input, predictor, classes, geofence, camera)
                )
