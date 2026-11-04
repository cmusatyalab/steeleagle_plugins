"""Uriel engine: receives the RawFrames (RGB24) that patchcore-score forwarded
as anomalous, runs RF-DETR looking for a person at confidence >= args['confidence'],
and forwards every frame on to patchcore-retrain unchanged -- tagging each with a
result JSON noting whether a person was actually found (true positive) or not
(false positive), plus the frame's own id for correlation. patchcore-retrain
subscribes to this engine to read that tag against the frame it separately
receives on the main edge.
"""
import json
import logging
import time

import cv2
import numpy as np
import uriel
from google.protobuf.message import DecodeError
from PIL import Image

from steeleagle_protocol.v1.messages.frame import frame_pb2

try:
    from rfdetr.util.coco_classes import COCO_CLASSES
except ImportError:
    from rfdetr.assets.coco_classes import COCO_CLASSES

logger = logging.getLogger("verifier")

PERSON_CLASS_ID = next(cid for cid, name in COCO_CLASSES.items() if name == "person")

RGB_CHANNELS = 3

# nano/small/medium/large ship in plain rfdetr; xlarge/2xlarge need
# "pip install rfdetr_plus" on top.
VARIANT_MAP = {
    "nano": ("rfdetr", "RFDETRNano"),
    "small": ("rfdetr", "RFDETRSmall"),
    "medium": ("rfdetr", "RFDETRMedium"),
    "large": ("rfdetr", "RFDETRLarge"),
    "xlarge": ("rfdetr_plus", "RFDETRXLarge"),
    "2xlarge": ("rfdetr_plus", "RFDETR2XLarge"),
}


def load_model(variant, model_path=None):
    if variant not in VARIANT_MAP:
        raise ValueError(f"unknown variant {variant!r}; choose from {sorted(VARIANT_MAP)}")
    module_name, class_name = VARIANT_MAP[variant]
    module = __import__(module_name, fromlist=[class_name])
    model_cls = getattr(module, class_name)

    logger.info("loading RF-DETR %s (checkpoint=%s)", class_name, model_path or "default COCO pretrained")
    model = model_cls(pretrain_weights=model_path) if model_path else model_cls()
    return model


def detect_people(model, frame_bgr: np.ndarray, conf: float):
    pil_image = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
    detections = model.predict(pil_image, threshold=conf)

    results = []
    for xyxy, score, cls_id in zip(detections.xyxy, detections.confidence, detections.class_id):
        if int(cls_id) != PERSON_CLASS_ID:
            continue
        x1, y1, x2, y2 = xyxy
        results.append({"score": float(score), "x_min": float(x1), "y_min": float(y1),
                         "x_max": float(x2), "y_max": float(y2)})
    return results


def run_engine(name, args):
    """Runs the verifier as the engine `name`. `args` has every arg of
    verifier.plugin.toml."""
    model_path = args["model_path"] or None
    model = load_model(args["variant"], model_path)

    with uriel.Engine(name, max_output_size=args["max_output_size"]) as engine:
        logger.info("engine %s ready", name)
        for input in engine:
            t0 = time.perf_counter()
            raw_frame = frame_pb2.RawFrame()
            try:
                raw_frame.ParseFromString(bytes(input.data))
            except DecodeError as e:
                logger.warning("input %d: not a RawFrame (%s); dropping", input.id.seq, e)
                engine.drop()
                continue

            width, height = raw_frame.h_res, raw_frame.v_res
            if raw_frame.channels != RGB_CHANNELS or len(raw_frame.data) != width * height * RGB_CHANNELS:
                logger.warning(
                    "input %d: %d bytes, not a %dx%d RGB frame; dropping",
                    input.id.seq, len(raw_frame.data), width, height,
                )
                engine.drop()
                continue

            rgb = np.frombuffer(raw_frame.data, dtype=np.uint8).reshape((height, width, RGB_CHANNELS))
            frame_bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

            detections = detect_people(model, frame_bgr, args["confidence"])
            elapsed_ms = (time.perf_counter() - t0) * 1000

            person_detected = bool(detections)
            result = json.dumps({"person_detected": person_detected, "frame_id": raw_frame.id}).encode()

            if person_detected:
                logger.info("person detected: %d (%.1fms) -> forward (TP)", len(detections), elapsed_ms)
            else:
                logger.info("no person (%.1fms) -> forward (FP)", elapsed_ms)

            engine.forward(result=result)
