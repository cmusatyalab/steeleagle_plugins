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
CONF_THRESHOLD = 0.70
FRAME_W, FRAME_H = 1280, 720
EXPECTED_RGB_SIZE = FRAME_W * FRAME_H * 3

VARIANT_MAP = {
    "nano": ("rfdetr", "RFDETRNano"),
    "small": ("rfdetr", "RFDETRSmall"),
    "medium": ("rfdetr", "RFDETRMedium"),
    "large": ("rfdetr", "RFDETRLarge"),
    "xlarge": ("rfdetr_plus", "RFDETRXLarge"),
    "2xlarge": ("rfdetr_plus", "RFDETR2XLarge"),
}

WINDOW_NAME = "detected person"
cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
cv2.resizeWindow(WINDOW_NAME, 1920, 1080)


def load_model(variant=VARIANT, model_path=None):
    module_name, class_name = VARIANT_MAP[variant]
    module = __import__(module_name, fromlist=[class_name])
    model_cls = getattr(module, class_name)
    model = model_cls(pretrain_weights=model_path) if model_path else model_cls()
    return model


def detect_people(model, frame_bgr, conf=CONF_THRESHOLD):
    pil_image = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
    detections = model.predict(pil_image, threshold=conf)
    results = []
    for box, cls_id, score in zip(detections.xyxy, detections.class_id, detections.confidence):
        if int(cls_id) != PERSON_CLASS_ID:
            continue
        x_min, y_min, x_max, y_max = box
        results.append({
            "score": float(score),
            "x_min": float(x_min), "y_min": float(y_min),
            "x_max": float(x_max), "y_max": float(y_max),
        })
    return results


def draw_detections(frame_bgr, detections):
    annotated = frame_bgr.copy()
    for d in detections:
        p1 = (int(d["x_min"]), int(d["y_min"]))
        p2 = (int(d["x_max"]), int(d["y_max"]))
        cv2.rectangle(annotated, p1, p2, (0, 255, 0), 2)
        label = f"person {d['score']:.2f}"
        cv2.putText(annotated, label, (p1[0], max(p1[1] - 8, 0)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
    return annotated


def main():
    model = load_model()
    with uriel.Engine(VERIFIER_ENGINE, max_output_size=MAX_FRAME_SIZE) as engine:
        for input in engine:
            frame_bytes = bytes(input.data)
            if len(frame_bytes) != EXPECTED_RGB_SIZE:
                engine.drop(result=b'{"error": "bad frame size"}')
                continue

            rgb = np.frombuffer(frame_bytes, dtype=np.uint8).reshape((FRAME_H, FRAME_W, 3))
            frame_bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

            detections = detect_people(model, frame_bgr)
            if detections:
                annotated = draw_detections(frame_bgr, detections)
                cv2.imshow(WINDOW_NAME, annotated)
                cv2.waitKey(1)
                engine.drop(result=b'{"person_detected": true}')
            else:
                # TODO: once the retrainer engine exists, forward here as a
                # potential false positive instead of dropping.
                engine.drop(result=b'{"person_detected": false}')


if __name__ == "__main__":
    main()
