"""Uriel engine: watches every frame patchcore-rfdetr-verifier forwards (both
true positives -- a real person confirmed -- and false positives -- score
flagged it anomalous but no person was actually there). Once FPs are
sustained at a high rate, it first tries swapping patchcore-score to a
better existing memory bank; if none of the existing banks fix it, it
collects a larger window of FP frames and retrains a new one, then pushes
it to fermi and tells patchcore-score to switch to it. True positives are
forwarded on unchanged to the encoder for display; false positives are not
(there's no real person in them -- nothing to show).

Verifier forwards every frame on its own primary edge (unchanged RawFrame
bytes) and separately tags each one with a result JSON --
{"person_detected": bool, "frame_id": int} -- delivered through this
engine's subscription to verifier (poll_event()/EventKind.RESULT), the same
mechanism patchcore-score already uses to watch this engine's own bank-swap
notifications. The frame_id lets this engine match a verifier result to the
exact frame it describes, rather than assuming the two independent streams
(verifier's primary edge, and verifier's result subscription) stay in lockstep
order -- they're delivered through genuinely separate channels, so pairing by
id is the only solid guarantee.

Candidate banks and newly-trained banks both live in steeleagle/models/
under XDG data home -- the same directory eagled's TransferService.
TransferModel writes into, and the same one shared read-only with engine
plugins on whichever machine is running (every machine runs its own eagled).
That means evaluate_candidates() here sees every bank lambda's own eagled
knows about, including ones this engine just trained itself.

This plugin's own models/ directory (checked into the repo alongside this
file) carries whatever banks it ships with by default -- e.g. an initial
"grass" bank. That bundled directory is NOT the persistent MODEL_DIR above;
it's static content that ships with the code. At startup,
bootstrap_bundled_models() copies anything bundled there into MODEL_DIR (and
pushes it to fermi) if it isn't already present, so a bundled default is a
real, reusable candidate from the very first retrain decision onward --
otherwise the drone would needlessly retrain terrain it already has a
perfectly good bank for, the first time it's revisited.

Retransmitting a bank to fermi only happens once per bank, ever. Every file
that ever lands in MODEL_DIR got there either via bootstrap_bundled_models()
or via _retrain() -- and both of those push it to fermi in the same breath
they write it locally (push_new_bank()). So if evaluate_candidates() later
finds that same bank already sitting in MODEL_DIR and it still passes, fermi
already has it: redeploying it (announce_existing_bank()) is just a filename
in a JSON blob, not another ~1.5MB over the wire.

All embedding extraction, training and FAISS-index building happens
in-process -- no subprocess calls to patchcore_train.py / build_faiss_meta.py.
The logic is ported directly from those scripts (both already proven --
patchcore_train.py has made two good checkpoints, build_faiss_meta.py
already produced gravel.faiss/gravel_meta.pt successfully this session) so
this reuses their exact code paths rather than re-deriving anything.

How patchcore-score finds out a swap happened: patchcore-score subscribes
directly to this engine (same subscribe()/poll_event() pattern h264_encoder
already uses to watch h264_decoder). There's no separate side-channel
producer -- the swap info rides out as the `result` on this engine's own
next ack (drop() or forward(), whichever this iteration's frame gets) after
a deploy succeeds. That's safe precisely because the old (still-misbehaving)
bank keeps producing FPs, and therefore keeps producing inputs for this loop
to attach a result to, right up until the moment the swap actually lands on
fermi -- the input flow doesn't dry up until after the notification that
causes it to dry up has already gone out. The retrain worker runs on its own
thread (so the main loop never stalls acking new frames while training runs)
and can't touch `engine` directly -- only the thread that owns a Uriel
object may use it -- so it hands the finished swap info to the main thread
through a plain thread-safe queue instead.

bootstrap_bundled_models() is score's half of the same bootstrap
retrain.py does on lambda: both sides get seeded with the same files under
the same names (this plugin's own bundled models/ directory, checked into
source control), so a swap announcement naming one of those bundled banks
never needs an actual transfer -- fermi already has it independently.

ASSUMPTIONS / OPEN QUESTIONS STILL FLAGGED:
- collect_embeddings() assumes Folder's dataloader iterates files in a
  deterministic (sorted-by-filename) order, and that train_batch_size=1
  means embedding_store gets exactly one entry per image, in that order.
  evaluate_candidates() at least checks this (aborts loudly if
  len(embeddings) != chunk_size) rather than silently misaligning, but the
  ordering assumption itself is still worth a sanity check the first time
  this actually runs.
- extract_threshold() is reconstructed from memory of this session's
  build_faiss_meta.py (which did work, producing gravel_meta.pt's correct
  13.9981 threshold) -- worth a side-by-side diff against that script if
  anything here looks off. Also: train_patchcore()'s AnomalibEngine is
  currently run with limit_val_batches=0 (added to fix a zero-length-subset
  crash with tiny windows), which disables the validation step that
  calibrates the adaptive threshold -- extract_threshold() will fail on any
  freshly-trained bank until this is addressed with a real val split sized
  for however many frames retrain-collection actually gathers. Flagged, not
  fixed, as of this patch.
- The no-retransmit invariant (announce_existing_bank() never re-sends)
  assumes fermi's persistent directory is never cleaned up or reset
  independently of lambda's -- if fermi's steeleagle/models/ ever gets
  wiped without lambda's also being wiped, a candidate this engine thinks
  fermi already has would actually be missing there. Nothing here detects
  that case; it just trusts the invariant.
- frame_id correlation (new in this patch): verifier's primary forwarded-
  frame edge and its result subscription are two genuinely separate uriel
  channels. In the overwhelmingly common case both arrive within the same
  poll tick (they're emitted together from the same engine.forward(result=)
  call on verifier's side), but if a result hasn't shown up yet by the time
  its matching frame is polled here, this engine falls back to treating that
  one frame as a false positive (conservative: it won't get shown, and it
  nudges the window rather than risk mis-displaying an unconfirmed frame) and
  logs a warning. _pending_verifier_results is also capped so an unmatched
  result left behind by a frame that got dropped upstream (bad size, etc.)
  can't grow the dict unboundedly.
"""
import json
import logging
import math
import os
import queue
import shutil
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import torch
import faiss
import grpc
import uriel
from anomalib.data import Folder
from anomalib.engine import Engine as AnomalibEngine
from anomalib.models import Patchcore
from torchvision.transforms.v2 import Resize

from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.services.daemon import transfer_pb2, transfer_pb2_grpc

logger = logging.getLogger("retrain")

FRAME_W, FRAME_H = 1280, 720
RGB_CHANNELS = 3
EXPECTED_RGB_SIZE = FRAME_W * FRAME_H * RGB_CHANNELS

# -- Backbone config (must match FramePipeline.hpp / the QNN export exactly;
# tied to the fixed hardware export, not a deploy-time knob) --
BACKBONE = "resnet18"
LAYERS = ["layer2", "layer3"]
MODEL_INPUT_SIZE = 256  # matches patchcore_train.py's --image_size default
DEFAULT_NUM_NEIGHBORS = 9

TRANSFER_CHUNK_SIZE = 1 * 1024 * 1024  # 1MB; our files (faiss index + meta) are a few MB at most

# How many unmatched verifier results to hold onto at most, so a frame that
# never shows up here (e.g. dropped upstream for a bad size) can't let this
# dict grow forever.
MAX_PENDING_RESULTS = 200

# Bundled defaults that ship with this plugin's own repo directory -- NOT
# the persistent MODEL_DIR below. bootstrap_bundled_models() copies/pushes
# anything here into persistent storage at startup if it isn't there yet.
BUNDLED_MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")


def steeleagle_models_dir():
    """The directory eagled's TransferService.TransferModel writes into
    (steeleagle/models/ under XDG data home), and that's shared read-only
    with engine plugins. Every machine (lambda included) runs its own
    eagled, so this is always a local path -- lambda's own copy."""
    xdg_data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(xdg_data_home, "steeleagle", "models")


MODEL_DIR = steeleagle_models_dir()


class _StopAfterCollection(Exception):
    """Raised from inside the subsample_embedding hook to abort engine.fit()
    right after capturing embeddings, before real coreset subsampling runs
    (we don't want a memory bank out of a 10-20 frame window)."""


@dataclass
class WindowEntry:
    is_fp: bool
    frame_rgb: np.ndarray            # HxWx3 uint8, kept so we can save it for embedding/retrain
    at: float = field(default_factory=time.monotonic)


class SlidingTrigger:
    """Count-based (not wall-clock-based) sliding window, so bandwidth
    variability doesn't distort the FP rate: the window just waits for
    M..N_MAX actual outcomes, however long that takes, with a staleness cap
    so an ancient frame doesn't count forever."""

    def __init__(self, n_max, m_min, rate, stale_s):
        self.n_max = n_max
        self.m_min = m_min
        self.rate = rate
        self.stale_s = stale_s
        self.q = deque()

    def push(self, entry: WindowEntry):
        self.q.append(entry)
        while len(self.q) > self.n_max:
            self.q.popleft()
        while self.q and (time.monotonic() - self.q[0].at) > self.stale_s:
            self.q.popleft()

    def check_trigger(self):
        if len(self.q) < self.m_min:
            return None
        fp_count = sum(1 for e in self.q if e.is_fp)
        if fp_count / len(self.q) >= self.rate:
            return list(self.q)
        return None

    def clear(self):
        self.q.clear()


def collect_embeddings(frames_dir, backbone=BACKBONE, layers=LAYERS, image_size=MODEL_INPUT_SIZE):
    """Runs the exact same Folder + Engine.fit() + subsample_embedding-hook
    pipeline patchcore_train.py uses, but intercepts model.model.embedding_store
    before subsampling and returns it directly instead of building a memory
    bank. Used for quick candidate-bank evaluation (we just need each
    window frame's raw embedding, not a trained bank).

    Returns a list of (num_patches, channels, flat_embedding) tuples, one per
    image in frames_dir, in the dataloader's iteration order.
    """
    work_dir = Path(tempfile.mkdtemp(prefix="embed_collect_work_"))
    datamodule = Folder(
        name="embed_collect",
        root=work_dir,
        normal_dir=str(Path(frames_dir).resolve()),
        augmentations=Resize((image_size, image_size)),
        train_batch_size=1,
        test_split_mode="none",
        val_split_mode="none",
        normal_split_ratio=0.0,
    )

    model = Patchcore(backbone=backbone, layers=layers)

    captured = []

    def capture_instead_of_subsample(sampling_ratio, embeddings=None):
        for e in model.model.embedding_store:
            captured.append(e.detach().cpu().numpy().astype(np.float32))
        model.model.embedding_store = []
        raise _StopAfterCollection()

    model.model.subsample_embedding = capture_instead_of_subsample

    engine = AnomalibEngine(max_epochs=1, limit_val_batches=0)
    try:
        engine.fit(model=model, datamodule=datamodule)
    except _StopAfterCollection:
        pass

    results = []
    for emb in captured:
        num_patches, channels = emb.shape
        results.append((num_patches, channels, emb.reshape(-1)))
    return results


def train_patchcore(image_dir, bank_size, backbone=BACKBONE, layers=LAYERS, image_size=MODEL_INPUT_SIZE):
    """Inlined port of patchcore_train.py's training logic: trains a fresh
    Patchcore model on image_dir and returns it with model.model.memory_bank
    populated at (approximately) bank_size vectors. In-process, no subprocess.

    NOTE: limit_val_batches=0 below disables validation, which is also where
    anomalib calibrates the adaptive threshold -- extract_threshold() will
    raise on the result until this has a real val split. Flagged in this
    file's module docstring; not fixed here.
    """
    work_dir = Path(tempfile.mkdtemp(prefix="retrain_work_"))
    datamodule = Folder(
        name="retrain",
        root=work_dir,
        normal_dir=str(Path(image_dir).resolve()),
        augmentations=Resize((image_size, image_size)),
        test_split_mode="none",
        val_split_mode="none",
        normal_split_ratio=0.0,
    )
    model = Patchcore(backbone=backbone, layers=layers)

    info = {}
    original_subsample = model.model.subsample_embedding

    def subsample_to_bank_size(sampling_ratio, embeddings=None):
        pool = sum(e.shape[0] for e in model.model.embedding_store)
        if pool == 0:
            return original_subsample(sampling_ratio)
        if bank_size > pool:
            raise ValueError(f"bank_size {bank_size} is larger than the pool of {pool} patch vectors")
        ratio = (bank_size + 0.25) / pool
        info["pool"], info["ratio"] = pool, ratio
        logger.info("pool = %d patch vectors, coreset ratio = %.8f, target bank = %d", pool, ratio, bank_size)
        return original_subsample(ratio)

    model.model.subsample_embedding = subsample_to_bank_size

    engine = AnomalibEngine(max_epochs=1, limit_val_batches=0)
    engine.fit(model=model, datamodule=datamodule)

    actual = model.model.memory_bank.shape[0]
    logger.info("memory bank: %d vectors", actual)
    if actual != bank_size:
        logger.warning("requested %d but got %d", bank_size, actual)

    return model, {"pool_size": info.get("pool"), "coreset_sampling_ratio": info.get("ratio")}


def extract_threshold(model):
    """Inlined port of build_faiss_meta.py's extract_threshold() -- pulls the
    trained F1AdaptiveThreshold value off the Lightning module."""
    for attr in ("image_threshold", "pixel_threshold"):
        th = getattr(model, attr, None)
        if th is not None and hasattr(th, "value"):
            return float(th.value)
    raise RuntimeError("couldn't find a trained threshold on the model (checked image_threshold/pixel_threshold)")


def build_faiss_bank(model, backbone=BACKBONE, layers=LAYERS, num_neighbors=DEFAULT_NUM_NEIGHBORS):
    """Inlined port of build_faiss_meta.py: builds a faiss.IndexFlatL2 from
    model.model.memory_bank and a meta dict matching what score.py /
    score_against_bank() expect -- entirely in memory, no file IO.
    """
    memory_bank = model.model.memory_bank.detach().cpu().numpy().astype(np.float32)
    faiss_index = faiss.IndexFlatL2(memory_bank.shape[1])
    faiss_index.add(memory_bank)

    threshold = extract_threshold(model)
    meta = {
        "threshold": threshold,
        "backbone": backbone,
        "layers": layers,
        "num_neighbors": num_neighbors,
    }
    return faiss_index, meta


def load_candidate_banks(model_dir=None):
    """Pairs every *.faiss with its *_meta.pt in steeleagle/models/ by
    matching filename stem."""
    model_dir = model_dir or MODEL_DIR
    if not os.path.isdir(model_dir):
        return []
    candidates = []
    for fname in os.listdir(model_dir):
        if not fname.endswith(".faiss"):
            continue
        stem = fname[: -len(".faiss")]
        meta_path = os.path.join(model_dir, f"{stem}_meta.pt")
        if os.path.exists(meta_path):
            candidates.append((stem, os.path.join(model_dir, fname), meta_path))
    return candidates


def score_against_bank(faiss_index, meta, embedding_flat, num_patches, channels):
    """Same scoring logic as score.py's score(), reused here for candidate
    evaluation. Returns (anomaly_score, is_anomalous)."""
    patches_np = embedding_flat.reshape(num_patches, channels)

    sq_distances, indices = faiss_index.search(patches_np, 1)
    distances = np.sqrt(np.clip(sq_distances, 0, None))

    patches = torch.from_numpy(patches_np)
    patch_scores = torch.from_numpy(distances).reshape(1, num_patches)
    locations = torch.from_numpy(indices).long().reshape(1, num_patches)
    embedding_batched = patches.reshape(1, num_patches, -1)

    memory_bank = faiss_index.reconstruct_n(0, faiss_index.ntotal)
    scoring_model = Patchcore(backbone=meta["backbone"], layers=meta["layers"],
                               pre_trained=False, num_neighbors=meta["num_neighbors"])
    scoring_model.model.memory_bank = torch.from_numpy(memory_bank)

    score_tensor = scoring_model.model.compute_anomaly_score(patch_scores, locations, embedding_batched)
    score = float(score_tensor.squeeze())
    return score, score > meta["threshold"]


def save_window_frames(window, out_dir):
    """Saves the window's frames to out_dir with sortable filenames, so
    collect_embeddings()'s dataloader order matches window order."""
    for i, entry in enumerate(window):
        path = os.path.join(out_dir, f"{i:05d}.png")
        cv2.imwrite(path, cv2.cvtColor(entry.frame_rgb, cv2.COLOR_RGB2BGR))


def evaluate_candidates(window, tolerance_fraction):
    """Tests the triggered window's FALSE-POSITIVE entries against every
    existing bank in steeleagle/models/. True positives are deliberately
    excluded here: a good candidate bank is *supposed* to keep flagging a
    real person as anomalous, so counting those against a candidate would
    make every bank look like it's failing. Embeddings are computed once for
    the FP subset and reused across every candidate -- only the lightweight
    FAISS search + compute_anomaly_score reruns per candidate, not the
    backbone.

    Returns the (stem, faiss_path, meta_path) of the first bank that passes,
    or None if none do (including if the window had no FP entries at all).
    """
    fp_entries = [e for e in window if e.is_fp]
    chunk_size = len(fp_entries)
    if chunk_size == 0:
        logger.info("triggered window has no FP entries; nothing to evaluate")
        return None

    tolerance = max(1, math.ceil(tolerance_fraction * chunk_size))

    with tempfile.TemporaryDirectory(prefix="eval_window_") as frames_dir:
        save_window_frames(fp_entries, frames_dir)
        embeddings = collect_embeddings(frames_dir)

    if len(embeddings) != chunk_size:
        logger.error("collected %d embeddings for %d FP frames; aborting evaluation",
                     len(embeddings), chunk_size)
        return None

    for stem, faiss_path, meta_path in load_candidate_banks():
        faiss_index = faiss.read_index(faiss_path)
        meta = torch.load(meta_path, map_location="cpu", weights_only=False)

        fp_count = 0
        for num_patches, channels, flat in embeddings:
            _, is_anomalous = score_against_bank(faiss_index, meta, flat, num_patches, channels)
            if is_anomalous:
                fp_count += 1
            if fp_count > tolerance:
                break

        logger.info("candidate %s: %d/%d still FP (tolerance %d)", stem, fp_count, chunk_size, tolerance)
        if fp_count <= tolerance:
            return stem, faiss_path, meta_path

    return None


def _chunk_requests(path, filename, request_cls, chunk_size=TRANSFER_CHUNK_SIZE):
    data = Path(path).read_bytes()
    total = max(1, math.ceil(len(data) / chunk_size))
    for i in range(total):
        chunk = data[i * chunk_size:(i + 1) * chunk_size]
        # Spec says filename is read from the first chunk; sending it on
        # every chunk costs nothing and avoids any ambiguity.
        yield request_cls(chunk=chunk, number=i + 1, total=total, filename=filename)


def transfer_model_to_fermi(local_path, remote_filename, daemon_addr):
    """Pushes one file to fermi's eagled daemon via
    TransferService.TransferModel -- lands in fermi's steeleagle/models/,
    shared read-only with patchcore-score. TODO: confirm whether the channel
    needs TLS/auth credentials instead of insecure."""
    with grpc.insecure_channel(daemon_addr) as channel:
        stub = transfer_pb2_grpc.TransferServiceStub(channel)
        requests = _chunk_requests(local_path, remote_filename, transfer_pb2.TransferModelRequest)
        last_received = None
        for resp in stub.TransferModel(requests):
            last_received = resp.received
        logger.info("transferred %s to fermi as %s (last chunk acked: %s)",
                     local_path, remote_filename, last_received)
        return last_received


def bootstrap_bundled_models():
    """Makes sure every bank bundled with this plugin's own repo directory
    (BUNDLED_MODELS_DIR, checked into source control) is also in lambda's
    persistent MODEL_DIR and on fermi. Without this, evaluate_candidates()
    -- which only looks in MODEL_DIR -- would never see a bundled default as
    a reusable candidate, and the drone would retrain terrain it already has
    a perfectly good bank for, the very first time that terrain is seen.

    Pairs already present in MODEL_DIR by filename are left alone --
    they've already been pushed to fermi (either by an earlier run of this
    same bootstrap, or by push_new_bank() during a real retrain), so this
    never re-sends anything that's already there.
    """
    for stem, src_faiss, src_meta in load_candidate_banks(BUNDLED_MODELS_DIR):
        fname = os.path.basename(src_faiss)
        meta_name = os.path.basename(src_meta)
        dst_faiss = os.path.join(MODEL_DIR, fname)
        dst_meta = os.path.join(MODEL_DIR, meta_name)

        if os.path.exists(dst_faiss) and os.path.exists(dst_meta):
            logger.info("bundled bank %s already in persistent storage; skipping", stem)
            continue

        os.makedirs(MODEL_DIR, exist_ok=True)
        shutil.copyfile(src_faiss, dst_faiss)
        shutil.copyfile(src_meta, dst_meta)
        logger.info("bootstrapped bundled bank %s into persistent storage", stem)


class RetrainWorker:
    """Runs evaluation/retrain off the main polling thread, so the engine's
    main loop never stalls acking new frames while this works. Hands the
    result of a successful deploy back to the main thread through
    `swap_results`, a plain thread-safe queue -- the worker thread must never
    touch the main thread's `engine` object directly (only the thread that
    owns a Uriel object may use it), so it can't call engine.drop()/forward()
    itself.
    """

    def __init__(self, daemon_addr, tolerance_fraction, retrain_collect_seconds, default_bank_size):
        self.daemon_addr = daemon_addr
        self.tolerance_fraction = tolerance_fraction
        self.retrain_collect_seconds = retrain_collect_seconds
        self.default_bank_size = default_bank_size
        self._collecting = False
        self._collect_dir = None
        self._collect_deadline = None
        self._collect_count = 0
        self._busy = threading.Event()
        self.swap_results = queue.Queue()

    def busy(self):
        return self._busy.is_set()

    def handle_trigger(self, window):
        if self._busy.is_set():
            logger.info("already retraining/evaluating; ignoring new trigger")
            return
        self._busy.set()
        threading.Thread(target=self._run, args=(window,), daemon=True).start()

    def feed_collecting(self, entry: WindowEntry):
        """Called from the main loop for every frame while in retrain-
        collection mode, so we don't lose frames arriving after the trigger
        fired but before the worker thread finishes evaluation. Only FP
        frames are actually saved -- a true positive is a real person, and
        training that into the "normal" bank would be exactly backwards."""
        if not self._collecting or not entry.is_fp:
            return
        path = os.path.join(self._collect_dir, f"fp_{self._collect_count:05d}.png")
        cv2.imwrite(path, cv2.cvtColor(entry.frame_rgb, cv2.COLOR_RGB2BGR))
        self._collect_count += 1

    def _run(self, window):
        try:
            found = evaluate_candidates(window, self.tolerance_fraction)
            if found is not None:
                stem, faiss_path, meta_path = found
                logger.info("existing bank %s passes; telling fermi to use it (no retransmit)", stem)
                self.announce_existing_bank(faiss_path, meta_path)
                return

            logger.info("no existing bank passes; entering retrain-collection mode")
            self._retrain(window)
        finally:
            self._busy.clear()

    def _retrain(self, seed_window):
        with tempfile.TemporaryDirectory(prefix="retrain_fp_") as image_dir:
            self._collect_dir = image_dir
            self._collect_count = 0
            for entry in seed_window:
                self.feed_collecting(entry)
            self._collecting = True
            self._collect_deadline = time.monotonic() + self.retrain_collect_seconds
            while time.monotonic() < self._collect_deadline:
                time.sleep(0.5)
            self._collecting = False

            logger.info("collected %d FP frames over %.0fs; training",
                        self._collect_count, self.retrain_collect_seconds)

            model, _ = train_patchcore(image_dir, bank_size=self.default_bank_size)
            faiss_index, meta = build_faiss_bank(model)

            os.makedirs(MODEL_DIR, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            out_faiss = os.path.join(MODEL_DIR, f"retrain_{ts}.faiss")
            out_meta = os.path.join(MODEL_DIR, f"retrain_{ts}_meta.pt")
            faiss.write_index(faiss_index, out_faiss)
            torch.save(meta, out_meta)

            self.push_new_bank(out_faiss, out_meta)

    def push_new_bank(self, faiss_path, meta_path):
        """A brand-new bank, just trained: fermi has never seen it, so it
        actually has to go over the wire. Every file that ends up in
        lambda's persistent MODEL_DIR passes through either here or
        bootstrap_bundled_models() -- which is what lets
        announce_existing_bank() below skip the retransmit later: anything
        already sitting in MODEL_DIR was already pushed to fermi when it
        was created."""
        transfer_model_to_fermi(faiss_path, os.path.basename(faiss_path), self.daemon_addr)
        transfer_model_to_fermi(meta_path, os.path.basename(meta_path), self.daemon_addr)
        self._announce(faiss_path, meta_path)

    def announce_existing_bank(self, faiss_path, meta_path):
        """A bank that already passed evaluate_candidates() against
        lambda's own MODEL_DIR. It went through push_new_bank() (or
        bootstrap_bundled_models()) the first time it showed up there, and
        fermi's persistent directory holds onto it just as durably as ours
        does -- so there's nothing left to send. Just tell fermi/score which
        filenames to swap to."""
        self._announce(faiss_path, meta_path)

    def _announce(self, faiss_path, meta_path):
        """Queues the swap info for the main thread to attach to its next
        ack (drop() or forward(), whichever this iteration's frame gets) --
        that's what patchcore-score's subscription actually sees."""
        swap_info = {
            "faiss_filename": os.path.basename(faiss_path),
            "meta_filename": os.path.basename(meta_path),
        }
        self.swap_results.put(swap_info)
        logger.info("queued swap notification: %s", swap_info)


def run_engine(name, args, remote_qualifier=None):
    """Runs the retrain engine as `name`. `args` has every arg of
    patchcore-retrain.plugin.toml. Subscribes to verifier (same machine, so
    no remote-qualifier joining needed for that target) to read its per-frame
    TP/FP result, correlated by frame_id against the frame this engine
    separately receives on its own main edge."""
    trigger = SlidingTrigger(
        n_max=args["n_max"], m_min=args["m_min"],
        rate=args["fp_rate_trigger"], stale_s=args["window_stale_seconds"],
    )
    worker = RetrainWorker(
        daemon_addr=args["daemon_addr"],
        tolerance_fraction=args["tolerance_fraction"],
        retrain_collect_seconds=args["retrain_collect_seconds"],
        default_bank_size=args["default_bank_size"],
    )

    bootstrap_bundled_models()

    pending_results = {}  # frame_id -> is_fp, from verifier's result subscription

    with uriel.Engine(name, max_output_size=args["max_output_size"]) as engine:
        engine.subscribe(args["verifier_name"])
        logger.info("engine %s ready; watching %s for per-frame TP/FP results", name, args["verifier_name"])

        while True:
            engine.wait(0.1)
            while (event := engine.poll_event()) is not None:
                if event.kind != uriel.EventKind.RESULT:
                    continue
                try:
                    result = json.loads(bytes(event.result))
                    pending_results[result["frame_id"]] = not result["person_detected"]
                    if len(pending_results) > MAX_PENDING_RESULTS:
                        pending_results.pop(next(iter(pending_results)))
                except Exception as e:
                    logger.error("bad verifier result, ignoring: %s", e)

            input = engine.poll()
            if input is None:
                continue

            raw_frame = frame_pb2.RawFrame()
            try:
                raw_frame.ParseFromString(bytes(input.data))
            except Exception as e:
                logger.warning("input %d: not a RawFrame (%s); dropping", input.id.seq, e)
                engine.drop()
                continue

            if len(raw_frame.data) != EXPECTED_RGB_SIZE:
                logger.warning("dropping: got %d bytes, expected %d", len(raw_frame.data), EXPECTED_RGB_SIZE)
                engine.drop()
                continue

            is_fp = pending_results.pop(raw_frame.id, None)
            if is_fp is None:
                logger.warning("no verifier result yet for frame_id=%d; treating as FP conservatively", raw_frame.id)
                is_fp = True

            frame_rgb = np.frombuffer(raw_frame.data, dtype=np.uint8).reshape((FRAME_H, FRAME_W, 3))
            entry = WindowEntry(is_fp=is_fp, frame_rgb=frame_rgb)

            trigger.push(entry)
            worker.feed_collecting(entry)

            window = trigger.check_trigger()
            if window is not None:
                logger.info("FP rate trigger fired on %d frames", len(window))
                worker.handle_trigger(window)
                trigger.clear()

            # Deliver a pending swap notification on this input's ack, if
            # the worker finished one since we last checked. This can't
            # stall: the old bank is still misbehaving (and therefore still
            # producing inputs here) right up until this notification is
            # what causes the swap that fixes it.
            swap_info = None
            try:
                swap_info = worker.swap_results.get_nowait()
            except queue.Empty:
                pass
            swap_result = json.dumps(swap_info).encode() if swap_info is not None else None
            if swap_info is not None:
                logger.info("delivering swap notification: %s", swap_info)

            # A true positive is a real detection -- forward it on unchanged
            # for the encoder/GCS to display. A false positive is not shown.
            if args["forward_all"] or not is_fp:
                engine.forward(result=swap_result)
            else:
                engine.drop(result=swap_result)
