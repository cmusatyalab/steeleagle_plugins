"""Scores frames of a video against a focus with SigLIP 2 and SAM 3.

    python tools/compare_focus.py VIDEO --out DIR [--every 1] [--truth 12-40,95-130]

Writes DIR/scores.csv, DIR/scores.png, and DIR/frames/<seconds>.jpg: each
sampled frame with SAM 3's masks and the scores drawn on it. With --truth,
the seconds the focus is in view, it prints each score's ROC AUC and the
accuracy at its best threshold.

SigLIP 2 gives one score per frame. SAM 3 gives its presence score, whether
the concept is in the frame at all, and the fraction of the frame its masks
cover. facebook/sam3 is gated: request access on Hugging Face and log in
with `hf auth login` first, or pass --models siglip2."""

import argparse
import csv
import time
from pathlib import Path

import cv2
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import torch
from PIL import Image
from transformers import AutoModel, AutoProcessor, Sam3Model, Sam3Processor

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


class SigLIP2:
    def __init__(self, text, model):
        self.processor = AutoProcessor.from_pretrained(model)
        self.model = AutoModel.from_pretrained(model).to(DEVICE).eval()
        self.text = self.processor(text=[text], padding="max_length", max_length=64,
                                   truncation=True, return_tensors="pt").to(DEVICE)

    def __call__(self, image):
        inputs = self.processor(images=[image], return_tensors="pt").to(DEVICE)
        logits = self.model(**self.text, **inputs).logits_per_image
        return {"siglip2": torch.sigmoid(logits).item()}, None


class SAM3:
    def __init__(self, text, model):
        self.processor = Sam3Processor.from_pretrained(model)
        self.model = Sam3Model.from_pretrained(model).to(DEVICE).eval()
        self.text = text

    def __call__(self, image):
        inputs = self.processor(images=image, text=self.text, return_tensors="pt").to(DEVICE)
        outputs = self.model(**inputs)
        masks = self.processor.post_process_instance_segmentation(
            outputs, threshold=0.5, mask_threshold=0.5,
            target_sizes=inputs["original_sizes"].tolist(),
        )[0]["masks"]
        mask = masks.any(0).cpu().numpy() if len(masks) else np.zeros(image.size[::-1], bool)
        presence = torch.sigmoid(outputs.presence_logits).item()
        return {"sam3_presence": presence, "sam3_coverage": float(mask.mean())}, mask


def sample(video, every):
    capture = cv2.VideoCapture(str(video))
    fps = capture.get(cv2.CAP_PROP_FPS)
    step = max(1, round(every * fps))
    index = 0
    while capture.grab():
        if index % step == 0:
            ok, bgr = capture.retrieve()
            if ok:
                yield index / fps, bgr
        index += 1


def annotate(bgr, scores, mask, width=640):
    if mask is not None:
        bgr = bgr.copy()
        bgr[mask] = (0.5 * bgr[mask] + (0, 0, 127)).astype(np.uint8)
    small = cv2.resize(bgr, (width, round(bgr.shape[0] * width / bgr.shape[1])))
    for i, (name, value) in enumerate(scores.items()):
        cv2.putText(small, f"{name} {value:.3f}", (8, 24 + 24 * i), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (255, 255, 255), 2)
    return small


def parse_truth(text):
    return [tuple(map(float, span.split("-"))) for span in text.split(",")] if text else []


def auc(scores, truth):
    pos = [s for s, t in zip(scores, truth) if t]
    neg = [s for s, t in zip(scores, truth) if not t]
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def best_threshold(scores, truth):
    return max(((np.mean([(s >= t) == y for s, y in zip(scores, truth)]), t) for t in set(scores)))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("video", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--every", type=float, default=1.0, help="seconds between frames scored")
    parser.add_argument("--siglip-text", default="an aerial drone photo of a building")
    parser.add_argument("--sam-text", default="building")
    parser.add_argument("--siglip-model", default="google/siglip2-base-patch16-naflex")
    parser.add_argument("--sam-model", default="facebook/sam3")
    parser.add_argument("--models", default="siglip2,sam3")
    parser.add_argument("--truth", default="", help="seconds the focus is in view, e.g. 12-40,95-130")
    args = parser.parse_args()

    models = {}
    if "siglip2" in args.models:
        models["siglip2"] = SigLIP2(args.siglip_text, args.siglip_model)
    if "sam3" in args.models:
        models["sam3"] = SAM3(args.sam_text, args.sam_model)
    (args.out / "frames").mkdir(parents=True, exist_ok=True)
    spans = parse_truth(args.truth)
    rows, elapsed = [], dict.fromkeys(models, 0.0)

    with torch.inference_mode():
        for t, bgr in sample(args.video, args.every):
            image = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
            row, mask = {"seconds": round(t, 2)}, None
            for name, model in models.items():
                start = time.perf_counter()
                scores, m = model(image)
                elapsed[name] += time.perf_counter() - start
                row.update(scores)
                mask = m if m is not None else mask
            if spans:
                row["truth"] = int(any(a <= t <= b for a, b in spans))
            rows.append(row)
            cv2.imwrite(str(args.out / "frames" / f"{t:07.1f}.jpg"),
                        annotate(bgr, {k: v for k, v in row.items() if k not in ("seconds", "truth")}, mask))
            print(row, flush=True)

    with open(args.out / "scores.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)

    names = [k for k in rows[0] if k not in ("seconds", "truth")]
    seconds = [r["seconds"] for r in rows]
    fig, ax = plt.subplots(figsize=(14, 4))
    for name in names:
        ax.plot(seconds, [r[name] for r in rows], label=name)
    for a, b in spans:
        ax.axvspan(a, b, color="green", alpha=0.1)
    ax.set(xlabel="seconds", ylabel="score", ylim=(0, 1))
    ax.legend()
    fig.tight_layout()
    fig.savefig(args.out / "scores.png", dpi=120)

    for name, total in elapsed.items():
        print(f"{name}: {1e3 * total / len(rows):.0f} ms per frame on {DEVICE}")
    if spans:
        truth = [r["truth"] for r in rows]
        for name in names:
            scores = [r[name] for r in rows]
            accuracy, threshold = best_threshold(scores, truth)
            print(f"{name}: AUC {auc(scores, truth):.3f}, accuracy {accuracy:.3f} at {threshold:.3f}")


if __name__ == "__main__":
    main()
