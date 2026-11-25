"""Text-guided tile selection with MobileCLIP: the encoders, the tiling, and
the send decision. Nothing here knows about Uriel, so the tools (quantizing,
offline evaluation) share it with the engine.

A frame is cut into overlapping square tiles, each tile is embedded, and its
relevance is s = v.t - max_j v.b_j: how much more it looks like the prompt t
than like the closest background b_j. A tile is worth sending when
s > max(0, thr + margin * std(s)), where thr is an EMA of the k-th best score
of each frame and std(s) is taken over the frame's tiles: it has to look more
like the target than like any background, and stand out from its own frame.
Learning (Selector) only ever narrows that: it drops tiles like a confirmed
background and puts tiles like a confirmed target first, but a tile that
fails the relevance test is never sent, however much it looks like a target.
"""
import os
import time
from pathlib import Path

import cv2
import numpy as np

# The plugin's folder (tools/ writes its own runs and models here).
PLUGIN_DIR = Path(__file__).resolve().parents[2]


def _models_dir():
    """The daemon's models directory: STEELEAGLE_MODELS, which eagled sets;
    /steeleagle/models, where it is mounted in a sandbox; or, run by hand
    (tests, tools), the daemon's default, ~/.local/share/steeleagle/models."""
    configured = os.environ.get("STEELEAGLE_MODELS")
    if configured and Path(configured).is_dir():
        return Path(configured)
    if Path("/steeleagle/models").is_dir():
        return Path("/steeleagle/models")
    return Path.home() / ".local/share/steeleagle/models"


# The encoders and the QNN libraries are too large for git: they are kept in
# cliped/ in the daemon's models directory, which holds
#   image_encoder_int8.tflite, distill_visual.pt, and libs/ (the QNN libraries)
MODELS_DIR = _models_dir() / "cliped"
LIBS_DIR = MODELS_DIR / "libs"
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
    background vector b_j for each of `backgrounds`. With no `prompt` (an
    image prompt), t is None."""
    t = None
    if prompt:
        t = encoder.encode_text([template.format(prompt) for template in TEMPLATES]).mean(0)
        t /= np.linalg.norm(t)
    b = encoder.encode_text([TEMPLATES[0].format(bg) for bg in backgrounds])
    return t, b


def image_vector(image_encoder, rgb):
    """The prompt vector t of the reference image `rgb` (uint8 RGB), embedded
    whole, squeezed to a square, by the encoder that embeds the tiles.

    Image-to-image similarities run well above the image-to-text ones of the
    backgrounds, so s is above 0 for most tiles: the adaptive threshold, not
    the 0 floor, decides what is sent."""
    tile = cv2.resize(rgb, (image_encoder.size, image_encoder.size), interpolation=cv2.INTER_AREA)
    t = image_encoder.encode_tiles([tile])[0].astype(np.float64)
    return t / np.linalg.norm(t)


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


# How many answered tiles' features a background keeps, newest last
MAX_FEATURES = 8

# What Selector.learn did with an answer
ADDED_GOOD = "good"
CONFIRMED_GOOD = "confirmed good"
DROPPED_GOOD = "dropped good"
ADDED_BACKGROUND = "background"
CONFIRMED = "confirmed"
KNOWN = "known"
GUARDED = "guarded"


class Selector:
    """The send decision. `t` and `b` are the prompt and background vectors;
    `k` is the send budget the adaptive threshold tracks, `ema` how slowly it
    moves, `margin` how far above it a tile must be in std(s), and at most
    `max_send` tiles of a frame are picked.

    With `learn`, it also learns from the verifier's yes or no about each
    tile it sent, keeping the tile's embedding: a yes in the good tiles, a no
    in the background tiles. They gate s rather than join it: tile-to-tile
    similarities (~0.8) dwarf a tile's to the prompt (~0.5 to a reference
    photo, ~0.25 to text), so a learned tile in s would outscore t
    everywhere. A tile at least `similarity` to a confirmed background tile,
    and closer to it than to any good tile, is dropped; one at least
    `good_similarity` to a confirmed good tile, and no closer to a background
    tile, goes ahead of the frame's other tiles and of the queue (must_send),
    but only if it passes the relevance test on s, as every tile must: in
    aerial video plain foliage is ~0.9 to a tile that held the target, so a
    good tile that forced its look-alikes out would send empty tiles frame
    after frame. Any other tile is decided by s as without learning.

    A good tile is confirmed the same way, once the verifier said yes
    `good_confirmations` times to tiles within `good_similarity` of it. Until
    then it is a candidate, which forces nothing: the verifier also says yes
    to the odd distractor, and with at most `max_send` tiles a frame, one
    wrong yes that forced its look-alikes would crowd the target out of every
    frame from then on. A target seen frame after frame is soon said yes to
    again. A no within `good_similarity` of a candidate counts against it,
    and once the no's reach its yes's the candidate is dropped (DROPPED_GOOD),
    so that tiles like it can be learned as background. `good_similarity` is
    above `similarity` because tiles of one scene are ~0.8 similar to each
    other: a good tile that close would force plain ground around the target.

    A background tile is confirmed once the verifier said no `confirmations`
    times to tiles within `similarity` of it: its first no, and each later no
    that is not added because it is that close. The verifier misses targets
    too, and one missed target confirmed at once would suppress the target
    from then on; a distractor seen in frame after frame is soon said no to
    again. For the same reason, a no is never learned (GUARDED) if it is at
    least as close to a good tile, candidate or confirmed, as to the
    background tiles, or within `similarity` of one. A tile is added only if
    it is below `similarity` (`good_similarity` for a good tile) to every tile
    in its set, and at most `max_learned` per set are kept, oldest dropped
    first.

    A caller that can tell a no's tile is visually a good tile (cliped's
    visual_guard: ORB keypoints matching a tile said yes to) passes that as
    learn's `guarded` instead: CLIP embeddings of aerial tiles are so alike
    (a no's tile of grass and a bag ~0.9 to a yes's of a mannequin in grass)
    that the cosine guard keeps almost every no from being learned. Its
    word then also decides whether a no counts against a candidate good
    tile: one of empty forest ~0.9 to the actor's would drop it.

    The same caller can hand learn each answered tile's `features` and
    score, select and passes an `unveto(i, features)`: a tile that a
    confirmed background would veto is kept if unveto says it is not
    visually any of those backgrounds' tiles (`features`, the ones within
    `similarity` of it). CLIP can't tell a tile with a small target from
    one without (~0.9 either way in aerial video), so learned empty water
    vetoed the swimmer; ORB keypoints only match the same patch seen
    again. select asks only about tiles that pass the relevance test."""

    def __init__(self, t, b, *, k=2, ema=0.9, margin=0.25, max_send=1, learn=False, similarity=0.85,
                 confirmations=2, max_learned=32, good_similarity=0.9, good_confirmations=2):
        self.t, self.b = t, b
        self.k, self.ema, self.margin, self.max_send = k, ema, margin, max_send
        self.learning, self.similarity, self.confirmations, self.max_learned = (
            learn, similarity, confirmations, max_learned)
        self.good_similarity, self.good_confirmations = good_similarity, good_confirmations
        self.thr = None
        self.good = []  # tiles said yes to, candidates and confirmed
        self.yeses = []  # how many yes's each good tile has had
        self.good_noes = []  # how many no's each good tile has had while a candidate
        self.backgrounds = []  # tiles said no to
        self.noes = []  # how many no's each background tile has had
        self.background_features = []  # the features (learn's) of the tiles each background's no's were about

    @property
    def confirmed_good(self):
        """The good tiles that force tiles like them to be sent."""
        return [x for x, yeses in zip(self.good, self.yeses) if yeses >= self.good_confirmations]

    def score(self, v, unveto=None):
        """For each embedding in `v`: its relevance s, and its verdict, +1 if
        it looks like a confirmed target (sent first, if s passes), -1 if it
        must not be sent, 0 if s alone decides. With `unveto` (see the
        class), a -1 it lifts is 0."""
        v = np.atleast_2d(v)
        s = v @ self.t - (v @ self.b.T).max(axis=1)
        verdict = np.zeros(len(v), dtype=int)
        if not (self.good or self.backgrounds):
            return s, verdict
        none = np.full(len(v), -np.inf)
        # Any good tile, candidate or not, keeps a tile like it from being
        # dropped; only a confirmed one forces it to be sent
        p = (v @ np.array(self.good).T).max(axis=1) if self.good else none
        good = self.confirmed_good
        pc = (v @ np.array(good).T).max(axis=1) if good else none
        n = (v @ np.array(self.backgrounds).T).max(axis=1) if self.backgrounds else none
        confirmed = [x for x, noes in zip(self.backgrounds, self.noes) if noes >= self.confirmations]
        nc = (v @ np.array(confirmed).T).max(axis=1) if confirmed else none
        verdict[(nc >= self.similarity) & (nc > p)] = -1
        verdict[(pc >= self.good_similarity) & (pc >= n)] = 1
        if unveto is not None:
            for i in np.flatnonzero(verdict == -1):
                if self.lifted(v[i], i, unveto):
                    verdict[i] = 0
        return s, verdict

    def lifted(self, x, i, unveto):
        """Whether `unveto` lifts the veto on the tile `i`, embedding `x`:
        it is asked with the features of the confirmed backgrounds within
        `similarity` of it."""
        near = [j for j, (b, noes) in enumerate(zip(self.backgrounds, self.noes))
                if noes >= self.confirmations and float(b @ x) >= self.similarity]
        return bool(unveto(i, [f for j in near for f in self.background_features[j]]))

    def relevance(self, v):
        """s for each embedding in `v`."""
        return self.score(v)[0]

    def passes(self, v, unveto=None):
        """For the queued tile with embedding `v`: whether it is still worth
        sending (s > 0, and its verdict is not -1), its s, and whether it goes
        first (its verdict is +1). `unveto` as in score."""
        s, verdict = self.score(v, unveto)
        return bool(verdict[0] != -1 and s[0] > 0), float(s[0]), bool(verdict[0] == 1)

    def select(self, v, unveto=None):
        """The indices of the tiles of one frame, with embeddings `v`, to send,
        best first; and the relevance s of every tile. `unveto` as in score,
        asked only about the relevant tiles vetoed."""
        s, verdict = self.score(v)
        k = min(self.k, len(s))
        kth = float(np.partition(s, -k)[-k])
        self.thr = kth if self.thr is None else self.ema * self.thr + (1 - self.ema) * kth
        relevant = s > max(0.0, self.thr + self.margin * s.std())
        if unveto is not None:
            for i in np.flatnonzero(relevant & (verdict == -1)):
                if self.lifted(v[i], i, unveto):
                    verdict[i] = 0
        # Relevant first: learning can drop a relevant tile or put it first,
        # never send one that isn't
        send = relevant & (verdict != -1)
        indices = np.flatnonzero(send)
        return indices[np.lexsort((-s[indices], -verdict[indices]))][: self.max_send], s

    def learn(self, x, match, guarded=None, counted=True, features=None):
        """Learns the verifier's answer `match` about the tile with embedding
        `x`; for a no, `guarded` (if not None) says whether the tile looks
        like a good tile, in place of the cosine guard. With `counted` off
        (the answer copied from a look-alike tile, not the verifier's own),
        the tile is only added: it never adds a yes or a no to a tile
        already learned, so one answer never confirms anything by itself.
        `features` (any value; None for none) are kept with the background a
        no is learned into, for score's unveto.
        Returns ADDED_GOOD or ADDED_BACKGROUND if the tile was added (a good one
        as a candidate); CONFIRMED_GOOD or CONFIRMED if the answer confirmed a
        good or a background tile, so that it now gates; DROPPED_GOOD if a no
        dropped a candidate good tile; KNOWN if it was too close to a tile in
        its set otherwise; GUARDED for a no that looks like a good tile (see
        the class). Only CONFIRMED_GOOD, CONFIRMED and DROPPED_GOOD (a
        candidate kept tiles like it from being dropped) change what passes."""
        x = np.asarray(x, dtype=np.float64)
        x = x / np.linalg.norm(x)
        good_sims = np.array([float(g @ x) for g in self.good])
        j = int(good_sims.argmax()) if len(good_sims) else None
        near_good = float(good_sims[j]) if j is not None else -np.inf
        if match:
            if near_good >= self.good_similarity:
                if self.yeses[j] >= self.good_confirmations or not counted:
                    return KNOWN
                self.yeses[j] += 1
                return CONFIRMED_GOOD if self.yeses[j] == self.good_confirmations else KNOWN
            self.good = [*self.good, x][-self.max_learned:]
            self.yeses = [*self.yeses, 1][-self.max_learned:]
            self.good_noes = [*self.good_noes, 0][-self.max_learned:]
            return CONFIRMED_GOOD if self.good_confirmations <= 1 else ADDED_GOOD
        if (near_good >= self.good_similarity and self.yeses[j] < self.good_confirmations
                and (guarded is None or guarded)):
            if not counted:
                return KNOWN
            self.good_noes[j] += 1
            if self.good_noes[j] >= self.yeses[j]:
                del self.good[j], self.yeses[j], self.good_noes[j]
                return DROPPED_GOOD
        sims = np.array([float(n @ x) for n in self.backgrounds])
        near_background = float(sims.max()) if len(sims) else np.inf
        if guarded if guarded is not None else near_good >= min(self.similarity, near_background):
            return GUARDED
        if len(sims) and near_background >= self.similarity:
            if not counted:
                return KNOWN
            j = int(sims.argmax())
            self.noes[j] += 1
            if features is not None:
                self.background_features[j] = [*self.background_features[j], features][-MAX_FEATURES:]
            return CONFIRMED if self.noes[j] == self.confirmations else KNOWN
        self.backgrounds = [*self.backgrounds, x][-self.max_learned:]
        self.noes = [*self.noes, 1][-self.max_learned:]
        self.background_features = [*self.background_features,
                                    [] if features is None else [features]][-self.max_learned:]
        return CONFIRMED if self.confirmations <= 1 else ADDED_BACKGROUND
