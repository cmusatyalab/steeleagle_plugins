#!/usr/bin/env python3
"""Offline evaluation of cliped on a labeled video: the engine's tiling,
encoders and send decision (cliped.clip), with an oracle for a verifier.
From the plugin folder:

    pixi run -e tools evaluate video.mp4 "a red tarp" --labels tools/labels/labels_street.txt

On the Tachyon, --npu runs an INT8 image encoder from quantize_clip.py on the
HTP; the text encoder stays on the CPU. QNN loads its libraries with dlopen, so
they must be on LD_LIBRARY_PATH when the process starts:

    LD_LIBRARY_PATH=libs pixi run -e tools evaluate video.mp4 "a red tarp" \\
        --npu models/image_encoder_int8.tflite --labels tools/labels/labels_street.txt

Without --npu the image encoder runs FP32 in PyTorch: the original MobileCLIP
weights, or --weights models/distill_visual.pt for the distilled ones.

With --npu the margin defaults to 0.25 instead of 1.0: INT8 scores rank tiles
less exactly than FP32, and the lower margin keeps recall (58/61 on the three
test videos, against 45/61 at 1.0).

Without --labels every verdict is "unsure": tiles are still saved to sent/ and
bytes are still counted, but tps/ stays empty and precision and recall are n/a.

A tile is sent when s > max(0, thr + margin * std(s)), where std(s) is taken
over the frame's tiles: it has to look more like the target than like any
background, and stand out from its own frame.

Differences from doc2.txt, for this mock:
  * No rejected bank and no feedback (step 5 and ON FEEDBACK): the verifier's
    verdicts never reach the drone.
  * The margin and the check against 0, which step 4 of doc2.txt does not have.
  * Selected tiles are sent as native-resolution crops (not the encoder's
    resized input), WebP quality 60 by default (--tile-codec, --tile-quality),
    counted in total_bytes_sent and saved to sent/; the oracle judges the whole
    frame, and tps/ holds whole frames.
  * The verifier is an oracle that looks the frame up in labels.txt.
  * Without --npu, everything runs FP32 on one machine.
  * No audit sends: with whole-frame transmission, a 1% per-tile audit over 28
    tiles would send about 25% of frames regardless of content.

Outputs: tps/ (frames with a verified TP), sent/ (every sent tile), stats.txt.
"""
import argparse
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import torch

from cliped.clip import BACKGROUNDS, LIBS_DIR, MODELS, Encoder, NpuImageEncoder, Selector, text_vectors, tile_boxes
from label_video import load_labels


def backend_setup(encoder, prompt):
    """Setup: returns the text vector t and background vectors {b_j}."""
    return text_vectors(encoder, prompt, BACKGROUNDS)


@dataclass
class Transmission:
    frame_id: int
    jpeg: bytes  # the whole frame, for the oracle
    tile_jpegs: dict  # tile position in the frame -> encoded tile (WebP by default), what the uplink carries


class Drone:
    def __init__(self, encoder, t, b, args):
        self.encoder = encoder
        self.t, self.b = t, b
        self.args = args
        self.selector = Selector(t, b, k=args.k, ema=args.ema, margin=args.margin, max_send=args.max_send)
        self.boxes = None

    def embed(self, frame_bgr):
        """Steps 1-2: tile the frame. Returns (BGR crops at native resolution, their embeddings).

        The encoder sees each crop resized to its input size; the crops themselves
        are what the uplink carries, so no bandwidth goes to upscaled pixels.
        """
        args = self.args
        if self.boxes is None:
            height, width = frame_bgr.shape[:2]
            self.boxes = tile_boxes(width, height, args.tile, args.stride)

        size = self.encoder.size
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        tiles = [cv2.resize(rgb[y0:y1, x0:x1], (size, size), interpolation=cv2.INTER_AREA)
                 for x0, y0, x1, y1 in self.boxes]
        crops = [frame_bgr[y0:y1, x0:x1] for x0, y0, x1, y1 in self.boxes]
        return crops, self.encoder.encode_tiles(tiles, args.batch_size)

    def select(self, v):
        """Steps 3-6: returns the indices of the tiles to send (cliped.clip.Selector)."""
        return self.selector.select(v)[0]

    def process(self, frame_id, frame_bgr, jpeg):
        """Steps 1-7. Returns a Transmission, or None if nothing is selected."""
        crops, v = self.embed(frame_bgr)
        selected = self.select(v)
        if selected.size == 0:
            return None
        tile_jpegs = {int(i): encode_tile(crops[i], self.args.tile_codec, self.args.tile_quality)
                      for i in selected}  # 7
        return Transmission(frame_id, jpeg, tile_jpegs)


class OracleVerifier:
    """Stands in for the backend verifier V, answering from a labels file."""

    def __init__(self, labels, tps_dir):
        self.labels = labels
        self.tps_dir = tps_dir

    def verify(self, tx):
        label = {"TP": "yes", "TN": "no"}.get(self.labels.get(tx.frame_id), "unsure")
        if label == "yes":
            (self.tps_dir / frame_name(tx.frame_id)).write_bytes(tx.jpeg)
        return label


def encode_jpeg(bgr, quality):
    return cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])[1].tobytes()


def frame_name(frame_id):
    return f"frame_{frame_id:06d}.jpg"


def encode_tile(bgr, codec, quality):
    """What the uplink carries per tile: WebP is about 40% smaller than JPEG at equal PSNR."""
    if codec == "jpeg":
        return encode_jpeg(bgr, quality)
    return cv2.imencode(".webp", bgr, [cv2.IMWRITE_WEBP_QUALITY, quality])[1].tobytes()


def tile_name(frame_id, position, codec="jpeg"):
    return f"frame_{frame_id:06d}_tile_{position:02d}.{'jpg' if codec == 'jpeg' else 'webp'}"


def downscale(frame, target_height):
    """Shrink to target_height, keeping aspect ratio; never upscales."""
    height, width = frame.shape[:2]
    if height <= target_height:
        return frame
    size = (round(width * target_height / height), target_height)
    return cv2.resize(frame, size, interpolation=cv2.INTER_AREA)


def read_frames(path, fps, max_frames, height):
    """Yield about fps (frame_id, frame) pairs per second of video.

    Frame IDs are sequential decode indices, as in label_video.py.
    """
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise SystemExit(f"could not open video: {path}")
    video_fps = cap.get(cv2.CAP_PROP_FPS)
    if video_fps <= 0:
        raise SystemExit(f"could not read the frame rate of {path}")
    every = max(round(video_fps / fps), 1)
    frame_id = yielded = 0
    while (max_frames is None or yielded < max_frames) and cap.grab():
        if frame_id % every == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            yield frame_id, downscale(frame, height)
            yielded += 1
        frame_id += 1
    cap.release()


class Stats:
    def __init__(self, labels):
        self.labels = labels
        self.processed = []
        self.sent = set()
        self.bytes_processed = 0
        self.bytes_sent = 0
        self.tiles_sent = 0
        self.verdicts = {"yes": 0, "no": 0, "unsure": 0}

    def report(self, args):
        positives = {i for i in self.processed if self.labels.get(i) == "TP"}
        negatives = {i for i in self.processed if self.labels.get(i) == "TN"}
        tp, fp = len(self.sent & positives), len(self.sent & negatives)
        fn = len(positives) - tp

        def ratio(numerator, denominator):
            return f"{numerator / denominator:.4f}" if denominator else "n/a"

        rows = [
            ("video", args.video),
            ("prompt", args.prompt),
            ("model", MODELS[args.model][0]),
            ("frames_processed", len(self.processed)),
            ("frames_sent", len(self.sent)),
            ("tiles_sent", self.tiles_sent),
            ("frames_unlabeled", len(self.processed) - len(positives) - len(negatives)),
            ("total_bytes_processed", self.bytes_processed),
            ("total_bytes_sent", self.bytes_sent),
            ("bytes_sent_fraction", ratio(self.bytes_sent, self.bytes_processed)),
            ("tp_sent", tp),
            ("fp_sent", fp),
            ("fn_not_sent", fn),
            ("tn_not_sent", len(negatives) - fp),
            ("precision", ratio(tp, tp + fp)),
            ("recall", ratio(tp, tp + fn)),
            ("verdicts_yes", self.verdicts["yes"]),
            ("verdicts_no", self.verdicts["no"]),
            ("verdicts_unsure", self.verdicts["unsure"]),
        ]
        return "".join(f"{key}: {value}\n" for key, value in rows)


def prepare_output_dir(path, overwrite):
    """Refuse to mix frames from an earlier run into this run's results."""
    path.mkdir(parents=True, exist_ok=True)
    stale = list(path.glob("frame_*.jpg"))
    if stale and not overwrite:
        raise SystemExit(f"{path}/ already holds {len(stale)} frames; pass --overwrite to replace them")
    for file in stale:
        file.unlink()


def parse_args():
    p = argparse.ArgumentParser(description="Text-guided edge filtering with an oracle verifier.")
    p.add_argument("video", type=Path)
    p.add_argument("prompt", help='target description, e.g. "a red tarp"')
    p.add_argument("--model", choices=MODELS, default="S0")
    p.add_argument("--labels", type=Path, help="per-frame labels from label_video.py (optional)")
    p.add_argument("--tps-dir", type=Path, default=Path("tps"))
    p.add_argument("--sent-dir", type=Path, default=Path("sent"))
    p.add_argument("--stats", type=Path, default=Path("stats.txt"))
    p.add_argument("--overwrite", action="store_true", help="replace frames from an earlier run")
    p.add_argument("--height", type=int, default=720, help="downscale frames to this height")
    p.add_argument("--tile", type=int, default=200, help="tile size in pixels")
    p.add_argument("--stride", type=int, help="tile stride in pixels (default: 0.75 * tile)")
    p.add_argument("--k", type=int, default=2, help="send budget for the adaptive threshold")
    p.add_argument("--max-send", type=int, default=4)
    p.add_argument("--ema", type=float, default=0.9)
    p.add_argument("--margin", type=float,
                   help="how far above the threshold a tile must be, in std(s) "
                        "(default: 1.0, or 0.25 with --npu, whose INT8 scores rank tiles less exactly)")
    p.add_argument("--jpeg-quality", type=int, default=85, help="full frames, for the oracle and bytes_processed")
    p.add_argument("--tile-codec", choices=["webp", "jpeg"], default="webp", help="codec for the tiles sent")
    p.add_argument("--tile-quality", type=int, default=60, help="tile quality (webp 60 ~ jpeg 70 in PSNR)")
    p.add_argument("--fps", type=float, default=1.0, help="frames per second of video to process")
    p.add_argument("--max-frames", type=int, help="stop after this many processed frames")
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--npu", type=Path, help="INT8 .tflite image encoder to run on the HTP")
    p.add_argument("--qnn-dir", type=Path, default=LIBS_DIR, help="QNN libraries for --npu")
    p.add_argument("--weights", type=Path, help="image encoder state dict for FP32, e.g. models/distill_visual.pt")
    p.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = p.parse_args()
    if args.margin is None:
        args.margin = 0.25 if args.npu else 1.0
    if args.stride is None:
        args.stride = max(int(args.tile * 0.75), 1)
    return args


def main():
    args = parse_args()
    if args.labels is not None and not args.labels.exists():
        raise SystemExit(f"{args.labels} not found; create it with label_video.py")
    labels = load_labels(args.labels) if args.labels is not None else {}
    prepare_output_dir(args.tps_dir, args.overwrite)
    prepare_output_dir(args.sent_dir, args.overwrite)

    encoder = Encoder(args.model, args.device, visual_weights=None if args.npu else args.weights)
    t, b = backend_setup(encoder, args.prompt)
    image_encoder = NpuImageEncoder(args.npu, args.qnn_dir) if args.npu else encoder
    drone = Drone(image_encoder, t, b, args)
    verifier = OracleVerifier(labels, args.tps_dir)
    stats = Stats(labels)
    recent = deque(maxlen=100)

    try:
        for frame_id, frame in read_frames(args.video, args.fps, args.max_frames, args.height):
            # Encoded for every frame so "bytes processed" is the cost of
            # sending everything, directly comparable to bytes sent.
            jpeg = encode_jpeg(frame, args.jpeg_quality)
            stats.processed.append(frame_id)
            stats.bytes_processed += len(jpeg)

            tx = drone.process(frame_id, frame, jpeg)
            recent.append(tx is not None)
            if tx is not None:
                for position, tile_jpeg in tx.tile_jpegs.items():
                    (args.sent_dir / tile_name(frame_id, position, args.tile_codec)).write_bytes(tile_jpeg)
                    stats.bytes_sent += len(tile_jpeg)
                stats.sent.add(frame_id)
                stats.tiles_sent += len(tx.tile_jpegs)
                label = verifier.verify(tx)
                stats.verdicts[label] += 1
                print(f"frame {frame_id}: sent {len(tx.tile_jpegs)} tiles -> {label}")
            if len(stats.processed) % 100 == 0:
                print(f"{len(stats.processed)} frames processed, "
                      f"{sum(recent)}% sent over the last {len(recent)}")
    except KeyboardInterrupt:
        print("interrupted; writing stats for the frames processed so far")
    finally:
        report = stats.report(args)
        args.stats.write_text(report)
        print(report, end="")
        if args.npu and image_encoder.tiles:
            print(f"npu_ms_per_tile: {1000 * image_encoder.seconds / image_encoder.tiles:.1f}")


if __name__ == "__main__":
    main()
