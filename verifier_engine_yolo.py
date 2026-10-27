"""Uriel engine: the final step. Receives the raw YUV420 frames h264-decode
reconstructed (output_type="", fixed FRAME_W x FRAME_H I420 -- same shape
decode already produces, so no upstream changes needed), runs YOLO26 Large
looking for a person at confidence >= 0.70, and shows the frame with the
box drawn on it if found.
"""
import time

import cv2
import numpy as np
import uriel
from ultralytics import YOLO

from common import MAX_FRAME_SIZE, VERIFIER_ENGINE

MODEL_PATH = "yolo26x.pt"
PERSON_CLASS_ID = 0  # COCO class id for "person"
CONF_THRESHOLD = 0.50
FRAME_W, FRAME_H = 1280, 720
EXPECTED_YUV_SIZE = FRAME_W * FRAME_H * 3 // 2

WINDOW_NAME = "detected person"
cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)


def load_model(device="cuda:0"):
    model = YOLO(MODEL_PATH)
    try:
        model.to(device)
        print(f"loaded {MODEL_PATH} on {device}", flush=True)
    except Exception as e:
        print(f"could not use device {device} ({e}); falling back to cpu", flush=True)
        model.to("cpu")
    return model


def detect_people(model, frame_bgr: np.ndarray, conf: float = CONF_THRESHOLD):
    results = model.predict(frame_bgr, conf=conf, classes=[PERSON_CLASS_ID], verbose=False)
    detections = []
    for result in results:
        for box in result.boxes:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            score = float(box.conf[0])
            detections.append({"score": score, "x_min": x1, "y_min": y1, "x_max": x2, "y_max": y2})
    return detections


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
