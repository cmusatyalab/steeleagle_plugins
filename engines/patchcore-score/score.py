"""Uriel engine: scores FrameWithEmbedding (from the C++ embed engine) against
the PatchCore memory bank via FAISS, forwards the frame as a RawFrame if
anomalous, drops it otherwise. Forwards no result. Scoring logic ported
verbatim from patchcore_csv_faiss.py.

The memory bank is reconstructed directly from the FAISS index rather than
loaded from a checkpoint -- the two used to carry the same vectors twice
(once in the ckpt's state_dict, once in the FAISS file). Only the threshold
and the small architecture config (backbone/layers/num_neighbors) come from
a slim metadata file now, not a full checkpoint.
"""

import logging
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


def run_engine(name, args):
    """Runs patchcore-score as the engine `name`. `args` has every arg of
    score.plugin.toml."""
    logger.info("loading FAISS index from %s", args["faiss_index"])
    faiss_index = faiss.read_index(args["faiss_index"])
    logger.info("FAISS index loaded: %d vectors", faiss_index.ntotal)

    logger.info("loading threshold/config from %s", args["threshold_meta"])
    meta = load_threshold_meta(args["threshold_meta"])
    trained_threshold = meta["threshold"]
    threshold = args["threshold"] or trained_threshold
    logger.info("using threshold=%.4f (trained=%.4f)", threshold, trained_threshold)

    model = load_model(
        faiss_index,
        backbone=meta["backbone"],
        layers=meta["layers"],
        num_neighbors=meta["num_neighbors"],
    )

    with torch.no_grad(), uriel.Engine(name, max_output_size=args["max_output_size"]) as engine:
        logger.info("engine %s ready", name)
        for input in engine:
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
