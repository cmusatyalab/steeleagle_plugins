"""Uriel engine: the final step. Receives the raw YUV420 frames h264-decode
reconstructed (output_type="", fixed FRAME_W x FRAME_H I420), runs RF-DETR
looking for a person at confidence >= 0.70, and shows the frame with the
box drawn on it if found.
"""
import time

import cv2
import numpy as np
import torch
import uriel
from PIL import Image

from common import MAX_FRAME_SIZE, VERIFIER_ENGINE

try:
    from rfdetr.util.coco_classes import COCO_CLASSES
except ImportError:
    from rfdetr.assets.coco_classes import COCO_CLASSES

PERSON_CLASS_ID = next(cid for cid, name in COCO_CLASSES.items() if name == "person")

VARIANT = "small"
CONF_THRESHOLD = 0.60
FRAME_W, FRAME_H = 1280, 720
EXPECTED_YUV_SIZE = FRAME_W * FRAME_H * 3 // 2

# nano/small/medium/large ship in plain rfdetr; xlarge/2xlarge need
# "pip install rfdetr_plus" on top.
VARIANT_MAP = {
    "nano": ("rfdetr", "RFDETRNano"),
    "small": ("rfdetr", "RFDETRSmall"),
    "medium": ("rfdetr", "RFDETRMedium"),
    "large": ("rfdetr", "RFDETRLarge"),
    "xlarge": ("rfdetr", "RFDETRXLarge"),
    "2xlarge": ("rfdetr", "RFDETR2XLarge"),
}

WINDOW_NAME = "detected person"
cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
cv2.resizeWindow(WINDOW_NAME, 1920, 1080)


def load_model(variant=VARIANT, model_path=None):
    if variant not in VARIANT_MAP:
        raise ValueError(f"unknown variant {variant!r}; choose from {sorted(VARIANT_MAP)}")
    package_name, class_name = VARIANT_MAP[variant]

    import importlib
    try:
        module = importlib.import_module(package_name)
        model_cls = getattr(module, class_name)
    except (ImportError, AttributeError) as e:
        raise ImportError(
            f"could not import {class_name} from {package_name}: {e}. "
            f"xlarge/2xlarge need: pip install rfdetr_plus"
        ) from e

    print(f"loading RF-DETR {class_name} (checkpoint={model_path or 'default COCO pretrained'})", flush=True)
    model = model_cls(pretrain_weights=model_path) if model_path else model_cls()
    return model


def detect_people(model, frame_bgr: np.ndarray, conf: float = CONF_THRESHOLD):
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


def main():
    model = load_model()

    with uriel.Engine(VERIFIER_ENGINE, max_output_size=MAX_FRAME_SIZE) as engine:
        print("verifier engine ready", flush=True)
        for input in engine:
            t0 = time.perf_counter()
            frame_bytes = bytes(input.data)

            if len(frame_bytes) != EXPECTED_YUV_SIZE:
                print(f"warning: {len(frame_bytes)} bytes, not a {FRAME_W}x{FRAME_H} I420 frame; dropping", flush=True)
                engine.drop()
                continue

            yuv = np.frombuffer(frame_bytes, dtype=np.uint8).reshape((FRAME_H * 3 // 2, FRAME_W))
            frame_bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)

            detections = detect_people(model, frame_bgr)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            if detections:
                annotated = draw_detections(frame_bgr, detections)
                cv2.imshow(WINDOW_NAME, annotated)
                cv2.waitKey(1)
                print(f"person detected: {len(detections)} ({elapsed_ms:.1f}ms) -> shown", flush=True)
                engine.drop(result=b'{"person_detected": true}')
            else:
                # TODO: once the retrainer engine exists, forward here as a
                # potential false positive instead of dropping.
                print(f"no person ({elapsed_ms:.1f}ms) -> dropped", flush=True)
                engine.drop(result=b'{"person_detected": false}')


if __name__ == "__main__":
    main()
