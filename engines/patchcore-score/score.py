"""Uriel engine: scores FrameWithEmbedding (from the C++ embed engine) against
the PatchCore memory bank via FAISS, forwards the frame as a RawFrame if
anomalous, drops it otherwise. Forwards no result. Scoring logic ported
verbatim from patchcore_csv_faiss.py.

The memory bank is reconstructed directly from the FAISS index rather than
loaded from a checkpoint -- the two used to carry the same vectors twice
(once in the ckpt's state_dict, once in the FAISS file). Only the threshold
and the small architecture config (backbone/layers/num_neighbors) come from
a slim metadata file now, not a full checkpoint.

Also subscribes to patchcore-retrain's engine (qualified name passed in via
STEELEAGLE_REMOTE_QUALIFIER, same contract h264_encoder uses to watch
h264_decoder) so it can hot-swap to a newly deployed bank without a restart.
A swap announcement is just a filename pair -- {"faiss_filename":...,
"meta_filename":...} -- resolved against this machine's own persistent
steeleagle/models/ directory. That only works because that directory
already has the named file: either retrain pushed it there the one time it
actually trained something new (TransferService.TransferModel), or it's
one of the bundled default banks bootstrap_bundled_models() below seeded
in independently, at startup, with zero network involved.

bootstrap_bundled_models() is score's half of the same bootstrap
retrain.py does on lambda: both sides get seeded with the same files under
the same names (this plugin's own bundled models/ directory, checked into
source control), so a swap announcement naming one of those bundled banks
never needs an actual transfer -- fermi already has it independently.
"""

import json
import logging
import os
import shutil
import time

import faiss
import numpy as np
import torch
import uriel
from anomalib.models import Patchcore

from frame_with_embedding_pb2 import FrameWithEmbedding
from steeleagle_protocol.v1.messages.frame import frame_pb2

logger = logging.getLogger("patchcore-score")

# The embed engine passes the decoded frame through as raw RGB24 bytes, with no
# dimensions, so they are fixed here (same as the verifier).
FRAME_W, FRAME_H = 1280, 720
RGB_CHANNELS = 3
EXPECTED_RGB_SIZE = FRAME_W * FRAME_H * RGB_CHANNELS

RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"

DEFAULT_BACKBONE = "resnet18"
DEFAULT_LAYERS = ["layer2", "layer3"]
DEFAULT_NUM_NEIGHBORS = 9

# Bundled defaults that ship with this plugin's own repo directory -- NOT
# the persistent steeleagle_models_dir() below. bootstrap_bundled_models()
# copies anything here into persistent storage at startup if it isn't
# there yet.
BUNDLED_MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")


def steeleagle_models_dir():
    """The directory eagled's TransferService.TransferModel writes into
    (steeleagle/models/ under XDG data home) -- same helper as retrain.py's,
    duplicated here since this is a separate plugin folder. Score runs on
    fermi, the same machine retrain's TransferModel calls push the new
    faiss+meta files to, so a swap notification's filenames resolve here
    directly."""
    xdg_data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(xdg_data_home, "steeleagle", "models")


def load_bank_pairs(model_dir):
    """Pairs every *.faiss with its *_meta.pt in model_dir by matching
    filename stem. Same pairing convention as retrain.py's
    load_candidate_banks(), duplicated here for the same reason
    steeleagle_models_dir() is."""
    if not os.path.isdir(model_dir):
        return []
    pairs = []
    for fname in os.listdir(model_dir):
        if not fname.endswith(".faiss"):
            continue
        stem = fname[: -len(".faiss")]
        meta_path = os.path.join(model_dir, f"{stem}_meta.pt")
        if os.path.exists(meta_path):
            pairs.append((stem, os.path.join(model_dir, fname), meta_path))
    return pairs


def bootstrap_bundled_models():
    """Makes sure every bank bundled with this plugin's own repo directory
    is also in fermi's own persistent steeleagle_models_dir() -- a local
    filesystem copy, nothing more, no network. This is score's half of the
    same bootstrap retrain.py does on lambda: both sides get seeded with
    the same files under the same names, which is what lets a future swap
    announcement from retrain name an existing candidate without ever
    transferring it -- fermi already has it, because this put it there
    independently."""
    models_dir = steeleagle_models_dir()
    for stem, src_faiss, src_meta in load_bank_pairs(BUNDLED_MODELS_DIR):
        fname = os.path.basename(src_faiss)
        meta_name = os.path.basename(src_meta)
        dst_faiss = os.path.join(models_dir, fname)
        dst_meta = os.path.join(models_dir, meta_name)

        if os.path.exists(dst_faiss) and os.path.exists(dst_meta):
            logger.info("bundled bank %s already in persistent storage; skipping", stem)
            continue

        os.makedirs(models_dir, exist_ok=True)
        shutil.copyfile(src_faiss, dst_faiss)
        shutil.copyfile(src_meta, dst_meta)
        logger.info("added bundled bank %s to persistent storage", stem)


def load_threshold_meta(meta_path):
    """Loads the slim metadata file (threshold + architecture config) that
    replaces the full checkpoint. Produced by the ckpt-to-faiss conversion
    script alongside the FAISS index."""
    meta = torch.load(meta_path, map_location="cpu", weights_only=False)
    if "threshold" not in meta:
        raise RuntimeError(f"no threshold found in {meta_path}")
    return {
        "threshold": float(meta["threshold"]),
        "backbone": meta.get("backbone", DEFAULT_BACKBONE),
        "layers": meta.get("layers", DEFAULT_LAYERS),
        "num_neighbors": meta.get("num_neighbors", DEFAULT_NUM_NEIGHBORS),
    }


def load_model(faiss_index, backbone, layers, num_neighbors):
    """Builds a Patchcore model with its memory bank reconstructed straight
    from the FAISS index. The backbone is never run here -- embeddings arrive
    precomputed from the C++ embed engine -- so its weights don't matter and
    are left at their random pre_trained=False init. Only model.model.memory_bank
    (used by compute_anomaly_score's reweighting step) needs to be real."""
    model = Patchcore(
        backbone=backbone,
        layers=layers,
        pre_trained=False,
        num_neighbors=num_neighbors,
    )
    memory_bank = faiss_index.reconstruct_n(0, faiss_index.ntotal)
    model.model.memory_bank = torch.from_numpy(memory_bank)
    model.eval()
    return model


def load_bank(faiss_path, meta_path, threshold_override):
    """Loads one (faiss_index, model, threshold, trained_threshold) bundle --
    used both for the initial load and for a hot-swap to a newly deployed
    bank."""
    logger.info("loading FAISS index from %s", faiss_path)
    faiss_index = faiss.read_index(faiss_path)
    logger.info("FAISS index loaded: %d vectors", faiss_index.ntotal)

    logger.info("loading threshold/config from %s", meta_path)
    meta = load_threshold_meta(meta_path)
    trained_threshold = meta["threshold"]
    threshold = threshold_override or trained_threshold
    logger.info("using threshold=%.4f (trained=%.4f)", threshold, trained_threshold)

    model = load_model(
        faiss_index,
        backbone=meta["backbone"],
        layers=meta["layers"],
        num_neighbors=meta["num_neighbors"],
    )
    return faiss_index, model, threshold, trained_threshold


def score(model, faiss_index, embedding_msg):
    """embedding_msg is a vartether.EmbeddingResult (data + shape, NHWC)."""
    embedding_flat = np.array(embedding_msg.data, dtype=np.float32)
    shape = list(embedding_msg.shape)

    if embedding_flat.size != int(np.prod(shape)):
        raise ValueError(f"data size {embedding_flat.size} doesn't match shape {shape}")

    _, h, w, c = shape
    num_patches = h * w
    patches_np = embedding_flat.reshape(num_patches, c)

    sq_distances, indices = faiss_index.search(patches_np, 1)
    distances = np.sqrt(np.clip(sq_distances, 0, None))

    patches = torch.from_numpy(patches_np)
    patch_scores = torch.from_numpy(distances).reshape(1, num_patches)
    locations = torch.from_numpy(indices).long().reshape(1, num_patches)
    embedding_batched = patches.reshape(1, num_patches, -1)

    score_tensor = model.model.compute_anomaly_score(patch_scores, locations, embedding_batched)
    return float(score_tensor.squeeze())


def run_engine(name, args, remote_qualifier=None):
    """Runs patchcore-score as the engine `name`. `args` has every arg of
    score.plugin.toml. `remote_qualifier`, if set, is patchcore-retrain's
    qualified name (from STEELEAGLE_REMOTE_QUALIFIER) -- this engine
    subscribes to it and hot-swaps its bank whenever retrain announces one."""
    bootstrap_bundled_models()

    faiss_index, model, threshold, trained_threshold = load_bank(
        args["faiss_index"], args["threshold_meta"], args["threshold"],
    )

    with torch.no_grad(), uriel.Engine(name, max_output_size=args["max_output_size"]) as engine:
        if remote_qualifier:
            engine.subscribe(remote_qualifier)
            logger.info("subscribed to %s for bank-swap notifications", remote_qualifier)

        logger.info("engine %s ready", name)
        for input in engine:
            event = engine.poll_event()
            if event is not None and event.type == uriel.EventType.RESULT:
                try:
                    swap_info = json.loads(bytes(event.data))
                    models_dir = steeleagle_models_dir()
                    new_faiss_path = os.path.join(models_dir, swap_info["faiss_filename"])
                    new_meta_path = os.path.join(models_dir, swap_info["meta_filename"])
                    faiss_index, model, threshold, trained_threshold = load_bank(
                        new_faiss_path, new_meta_path, args["threshold"],
                    )
                    logger.info("swapped to new bank: %s", swap_info)
                except Exception as e:
                    logger.error("bank swap failed, keeping current bank: %s", e)

            t0 = time.perf_counter()
            combined = FrameWithEmbedding()
            combined.ParseFromString(bytes(input.data))

            try:
                anomaly_score = score(model, faiss_index, combined.embedding)
            except ValueError as e:
                logger.error("bad embedding, dropping: %s", e)
                engine.drop()
                continue

            is_anomalous = anomaly_score > threshold
            elapsed_ms = (time.perf_counter() - t0) * 1000

            if not is_anomalous:
                engine.drop()
                logger.info("normal    score=%.4f (%.1fms) -> dropped", anomaly_score, elapsed_ms)
                continue

            frame_bytes = combined.encoded_frame
            if len(frame_bytes) != EXPECTED_RGB_SIZE:
                logger.error(
                    "ANOMALOUS score=%.4f but frame is %d bytes, not a %dx%d RGB frame; dropping",
                    anomaly_score, len(frame_bytes), FRAME_W, FRAME_H,
                )
                engine.drop()
                continue

            payload = frame_pb2.RawFrame(
                data=frame_bytes, h_res=FRAME_W, v_res=FRAME_H, channels=RGB_CHANNELS,
            ).SerializeToString()
            if len(payload) > args["max_output_size"]:
                logger.error(
                    "ANOMALOUS score=%.4f but RawFrame of %d bytes exceeds max_output_size %d; dropping",
                    anomaly_score, len(payload), args["max_output_size"],
                )
                engine.drop()
                continue

            engine.loan()[: len(payload)] = payload
            engine.replace(len(payload), output_type=RAW_FRAME_TYPE)
            logger.info("ANOMALOUS score=%.4f (%.1fms) -> forwarded", anomaly_score, elapsed_ms)
