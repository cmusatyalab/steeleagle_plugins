"""Text-guided tile selection with MobileCLIP: the encoders, the tiling, and
the send decision. Nothing here knows about Uriel, so the tools (quantizing,
offline evaluation) share it with the engine.

A frame is cut into overlapping square tiles, each tile is embedded, and its
relevance is s = v.t - max_j v.b_j: how much more it looks like the prompt t
than like the closest background b_j. A tile is worth sending when
s > max(0, thr + margin * std(s)), where thr is an EMA of the k-th best score
of each frame and std(s) is taken over the frame's tiles: it has to look more
like the target than like any background, and stand out from its own frame.
"""
import time
from pathlib import Path

import cv2
import numpy as np

# The plugin's folder, which holds models/ and libs/.
PLUGIN_DIR = Path(__file__).resolve().parents[2]
MODELS_DIR = PLUGIN_DIR / "models"
LIBS_DIR = PLUGIN_DIR / "libs"
# The INT8 image encoder quantize_clip.py builds from the distilled weights,
# for the HTP; and the distilled FP32 image encoder it was built from.
QUANTIZED_MODEL = MODELS_DIR / "image_encoder_int8.tflite"
FP32_WEIGHTS = MODELS_DIR / "distill_visual.pt"

# No MobileCLIP2-S1 exists, so S1 is the original MobileCLIP release. The
# models in models/ are S0.
MODELS = {
    "S0": ("MobileCLIP2-S0", "dfndr2b"),
    "S1": ("MobileCLIP-S1", "datacompdr"),
    "S2": ("MobileCLIP2-S2", "dfndr2b"),
}
DEFAULT_MODEL = "S0"

# Filled with the prompt verbatim, so write it as "a red tarp" or "two cars".
TEMPLATES = [
    "top-down aerial photo of {}",
    "drone photo of {}",
    "overhead view of {}",
    "aerial photograph showing {}",
    "a photo of {} seen from above",
    "high altitude image of {}",
]
BACKGROUNDS = ["grass", "dirt", "rocks", "trees", "a road", "a roof"]


class Encoder:
    """MobileCLIP in PyTorch: the text encoder, and the FP32 image encoder.
    `visual_weights` replaces the image encoder's weights with a state dict of
    the reparameterized model's `visual`, e.g. distilled ones."""

    def __init__(self, variant=DEFAULT_MODEL, device="cpu", visual_weights=None):
        import open_clip
        import torch
        from timm.utils import reparameterize_model

        name, pretrained = MODELS[variant]
        model, _, _ = open_clip.create_model_and_transforms(name, pretrained=pretrained, device=device)
        cfg = open_clip.get_model_preprocess_cfg(model)
        size = cfg["size"]
        self.size = size if isinstance(size, int) else size[0]
        self.mean = torch.tensor(cfg["mean"], device=device).view(1, 3, 1, 1)
        self.std = torch.tensor(cfg["std"], device=device).view(1, 3, 1, 1)
        self.tokenizer = open_clip.get_tokenizer(name)
        self.model = reparameterize_model(model.eval())  # same output, faster
        if visual_weights is not None:
            self.model.visual.load_state_dict(torch.load(visual_weights, map_location=device))
        self.device = device
        self._torch = torch

    def encode_text(self, texts):
        with self._torch.inference_mode():
            tokens = self.tokenizer(texts).to(self.device)
            return self.model.encode_text(tokens, normalize=True).float().cpu().numpy()

    def encode_tiles(self, tiles, batch_size=32):
        """Embed uint8 RGB tiles of shape (size, size, 3)."""
        torch = self._torch
        out = []
        with torch.inference_mode():
            for start in range(0, len(tiles), batch_size):
                batch = torch.from_numpy(np.stack(tiles[start : start + batch_size])).to(self.device)
                batch = batch.permute(0, 3, 1, 2).float().div(255).sub(self.mean).div(self.std)
                out.append(self.model.encode_image(batch, normalize=True).float().cpu().numpy())
        return np.concatenate(out)


class NpuImageEncoder:
    """The INT8 image encoder from quantize_clip.py. On the HTP through the
    QNN TFLite delegate in `qnn_dir`, or on the CPU if `qnn_dir` is None.

    QNN loads its libraries with dlopen, so `qnn_dir` must be on
    LD_LIBRARY_PATH when the process starts.
    """

    def __init__(self, model_path, qnn_dir=LIBS_DIR, num_threads=4):
        from ai_edge_litert.interpreter import Interpreter, load_delegate

        delegates = []
        if qnn_dir is not None:
            qnn_dir = Path(qnn_dir).resolve()
            delegates.append(load_delegate(str(qnn_dir / "libQnnTFLiteDelegate.so"), {
                "backend_type": "htp",
                "library_path": str(qnn_dir / "libQnnHtp.so"),
                "skel_library_dir": str(qnn_dir),
                "htp_performance_mode": "2",  # burst
                "htp_precision": "0",  # quantized
            }))
        self.interpreter = Interpreter(str(model_path), experimental_delegates=delegates, num_threads=num_threads)
        self.interpreter.allocate_tensors()
        self.input = self.interpreter.get_input_details()[0]
        self.output = self.interpreter.get_output_details()[0]
        self.size = int(self.input["shape"][1])
        self.tiles = 0
        self.seconds = 0.0

    def encode_tiles(self, tiles, batch_size=None):
        """Embed uint8 RGB tiles one at a time: the graph has batch size 1."""
        scale, zero_point = self.output["quantization"]
        out = np.empty((len(tiles), self.output["shape"][-1]), dtype=np.float32)
        start = time.perf_counter()
        for i, tile in enumerate(tiles):
            pixels = (tile.astype(np.int16) - 128).astype(np.int8)
            self.interpreter.set_tensor(self.input["index"], pixels[None])
            self.interpreter.invoke()
            out[i] = self.interpreter.get_tensor(self.output["index"])[0]
        self.seconds += time.perf_counter() - start
        self.tiles += len(tiles)
        out = scale * (out - zero_point)
        return out / np.linalg.norm(out, axis=1, keepdims=True)


def text_vectors(encoder, prompt, backgrounds=BACKGROUNDS):
    """The text vector t of `prompt`, averaged over TEMPLATES, and a
    background vector b_j for each of `backgrounds`."""
    t = encoder.encode_text([template.format(prompt) for template in TEMPLATES]).mean(0)
    t /= np.linalg.norm(t)
    b = encoder.encode_text([TEMPLATES[0].format(bg) for bg in backgrounds])
    return t, b


def tile_boxes(width, height, tile, stride):
    """Overlapping square tiles (x0, y0, x1, y1), with an edge-aligned last row
    and column. A tile larger than the frame shrinks to fit it."""
    tile = min(tile, width, height)

    def starts(extent):
        positions = list(range(0, extent - tile + 1, stride))
        if positions[-1] != extent - tile:
            positions.append(extent - tile)
        return positions

    return [(x, y, x + tile, y + tile) for y in starts(height) for x in starts(width)]


def cut_tiles(rgb, boxes, size):
    """The tiles `boxes` of the RGB frame `rgb`, each resized to the encoder's
    `size` x `size`."""
    return [cv2.resize(rgb[y0:y1, x0:x1], (size, size), interpolation=cv2.INTER_AREA) for x0, y0, x1, y1 in boxes]


class Selector:
    """The send decision. `t` and `b` are the text and background vectors;
    `k` is the send budget the adaptive threshold tracks, `ema` how slowly it
    moves, `margin` how far above it a tile must be in std(s), and at most
    `max_send` tiles of a frame are picked."""

    def __init__(self, t, b, *, k=2, ema=0.9, margin=0.25, max_send=1):
        self.t, self.b = t, b
        self.k, self.ema, self.margin, self.max_send = k, ema, margin, max_send
        self.thr = None

    def relevance(self, v):
        """s for each embedding in `v`."""
        return v @ self.t - (v @ self.b.T).max(axis=1)

    def select(self, v):
        """The indices of the tiles of one frame, with embeddings `v`, to send,
        best first; and the relevance s of every tile."""
        s = self.relevance(v)
        k = min(self.k, len(s))
        kth = float(np.partition(s, -k)[-k])
        self.thr = kth if self.thr is None else self.ema * self.thr + (1 - self.ema) * kth
        indices = np.flatnonzero(s > max(0.0, self.thr + self.margin * s.std()))
        return indices[np.argsort(-s[indices])][: self.max_send], s
