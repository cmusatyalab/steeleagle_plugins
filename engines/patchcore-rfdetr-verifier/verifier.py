"""Uriel engine: the final step. Receives the RawFrames (RGB24) that
patchcore-score forwarded as anomalous, runs RF-DETR looking for a person at
confidence >= args['confidence'], and shows the frame with the box drawn on it
if found (when args['show_window'] is set).
"""
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

WINDOW_NAME = "detected person"

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


def draw_detections(frame_bgr: np.ndarray, detections):
    out = frame_bgr.copy()
    for det in detections:
        x1, y1, x2, y2 = (int(det["x_min"]), int(det["y_min"]), int(det["x_max"]), int(det["y_max"]))
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 255, 0), 2)
        label = f"person {det['score']:.2f}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.rectangle(out, (x1, y1 - th - 8), (x1 + tw + 4, y1), (0, 255, 0), -1)
        cv2.putText(out, label, (x1 + 2, y1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)
    return out


def run_engine(name, args):
    """Runs the verifier as the engine `name`. `args` has every arg of
    verifier.plugin.toml."""
    model_path = args["model_path"] or None
    model = load_model(args["variant"], model_path)

    if args["show_window"]:
        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(WINDOW_NAME, 1920, 1080)

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

            if detections:
                if args["show_window"]:
                    annotated = draw_detections(frame_bgr, detections)
                    cv2.imshow(WINDOW_NAME, annotated)
                    cv2.waitKey(1)
                logger.info("person detected: %d (%.1fms) -> shown", len(detections), elapsed_ms)
                engine.drop(result=b'{"person_detected": true}')
            else:
                # TODO: once the retrainer engine exists, forward here as a
                # potential false positive instead of dropping.
                logger.info("no person (%.1fms) -> dropped", elapsed_ms)
                engine.drop(result=b'{"person_detected": false}')
