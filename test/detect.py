"""Quick standalone YOLO person detector -- not yet wired into a Uriel
engine. Loads YOLO26 Large, runs it on an image (or a folder of images, or
raw I420 YUV420 frames), and draws + saves detections for the 'person'
class with confidence >= 0.70.

Usage:
    python3 detector.py path/to/image.jpg
    python3 detector.py path/to/frame.yuv --raw
    python3 detector.py path/to/folder/ --device cpu
"""
import argparse
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

MODEL_PATH = "yolo26l.pt"
PERSON_CLASS_ID = 0  # COCO class id for "person"
CONF_THRESHOLD = 0.70
FRAME_W, FRAME_H = 1280, 720


def load_frame(path: Path, raw: bool) -> np.ndarray:
    if raw:
        data = path.read_bytes()
        expected = FRAME_W * FRAME_H * 3 // 2
        if len(data) != expected:
            raise ValueError(f"{path}: {len(data)} bytes, expected {expected} for {FRAME_W}x{FRAME_H} I420")
        yuv = np.frombuffer(data, dtype=np.uint8).reshape((FRAME_H * 3 // 2, FRAME_W))
        return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
    img = cv2.imread(str(path))
    if img is None:
        raise ValueError(f"failed to read {path}")
    return img


def detect_people(model, frame_bgr: np.ndarray, conf: float = CONF_THRESHOLD):
    """Runs the model and returns only 'person' detections at or above
    conf, as a list of dicts with pixel-space bbox coords."""
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
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="image file, raw .yuv file, or a folder of either")
    ap.add_argument("--raw", action="store_true", help="input is raw I420 YUV420 (fixed 1280x720)")
    ap.add_argument("--device", default="cuda:0", help="torch device, e.g. 'cuda:0' or 'cpu'")
    ap.add_argument("--conf", type=float, default=CONF_THRESHOLD)
    ap.add_argument("--save-dir", default="detector_out")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    print(f"loading {MODEL_PATH} on {args.device}", flush=True)
    model = YOLO(MODEL_PATH)
    try:
        model.to(args.device)
    except Exception as e:
        print(f"could not use device {args.device} ({e}); falling back to cpu", flush=True)
        model.to("cpu")

    path = Path(args.path)
    if path.is_dir():
        paths = sorted(p for p in path.glob("*.yuv" if args.raw else "*") if p.is_file())
    else:
        paths = [path]

    out_dir = Path(args.save_dir)
    out_dir.mkdir(exist_ok=True)

    for p in paths:
        frame = load_frame(p, args.raw)
        detections = detect_people(model, frame, conf=args.conf)
        if detections:
            print(f"{p.name}: {len(detections)} person(s) >= {args.conf}", flush=True)
            annotated = draw_detections(frame, detections)
            cv2.imwrite(str(out_dir / f"{p.stem}_detected.jpg"), annotated)
            if args.show:
                cv2.imshow("detections", annotated)
                cv2.waitKey(0)
        else:
            print(f"{p.name}: no person >= {args.conf}", flush=True)


if __name__ == "__main__":
    main()
