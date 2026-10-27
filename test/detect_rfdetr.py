"""Quick standalone RF-DETR person detector -- same shape as detect.py, not
wired into a Uriel engine yet. Loads an RF-DETR variant, runs it on an
image (or a folder, or raw I420 YUV420 frames), and draws + saves
detections for the 'person' class with confidence >= 0.70.

xlarge/2xlarge require the separately-licensed `rfdetr_plus` package
(pip install rfdetr_plus); nano/small/medium/large ship in plain `rfdetr`.

Usage:
    python3 detect_rfdetr.py path/to/image.jpg --variant large
    python3 detect_rfdetr.py path/to/frame.yuv --raw --variant large
    python3 detect_rfdetr.py path/to/folder/ --variant 2xlarge
"""
import argparse
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

CONF_THRESHOLD = 0.70
FRAME_W, FRAME_H = 1280, 720
try:
    from rfdetr.util.coco_classes import COCO_CLASSES
except ImportError:
    from rfdetr.assets.coco_classes import COCO_CLASSES

PERSON_CLASS_ID = next(cid for cid, name in COCO_CLASSES.items() if name == "person")

VARIANT_MAP = {
    "nano": ("rfdetr", "RFDETRNano"),
    "small": ("rfdetr", "RFDETRSmall"),
    "medium": ("rfdetr", "RFDETRMedium"),
    "large": ("rfdetr", "RFDETRLarge"),
    "xlarge": ("rfdetr", "RFDETRXLarge"),      # requires rfdetr_plus
    "2xlarge": ("rfdetr", "RFDETR2XLarge"),    # requires rfdetr_plus
}


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


def load_model(variant: str, model_path: str | None):
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
    if model_path:
        return model_cls(pretrain_weights=model_path)
    return model_cls()


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
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="image file, raw .yuv file, or a folder of either")
    ap.add_argument("--raw", action="store_true", help="input is raw I420 YUV420 (fixed 1280x720)")
    ap.add_argument("--variant", default="large", choices=sorted(VARIANT_MAP))
    ap.add_argument("--model", default=None, help="checkpoint path; omit for default COCO pretrained")
    ap.add_argument("--conf", type=float, default=CONF_THRESHOLD)
    ap.add_argument("--save-dir", default="detector_out_rfdetr")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    model = load_model(args.variant, args.model)

    path = Path(args.path)
    paths = sorted(p for p in path.glob("*.yuv" if args.raw else "*") if p.is_file()) if path.is_dir() else [path]

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
