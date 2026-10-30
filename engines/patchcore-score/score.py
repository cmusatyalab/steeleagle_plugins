"""Uriel engine: scores FrameWithEmbedding (from the C++ embed engine) against
the PatchCore memory bank via FAISS, forwards the frame as a RawFrame if
anomalous, drops it otherwise. Forwards no result. Scoring logic ported
verbatim from patchcore_csv_faiss.py.
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


def load_model_and_threshold(ckpt_path):
    ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    backbone = ckpt.get("backbone", "resnet18")
    layers = ckpt.get("layers", ["layer2", "layer3"])
    model = Patchcore(backbone=backbone, layers=layers, pre_trained=False)
    model.load_state_dict(ckpt["state_dict"])
    model.eval()

    threshold = None
    for k, v in ckpt["state_dict"].items():
        if k.endswith("post_processor._image_threshold"):
            threshold = float(v)
            break
    if threshold is None:
        raise RuntimeError(f"no post_processor._image_threshold found in {ckpt_path}")

    return model, threshold


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
    logger.info("loading PatchcoreModel (incl. memory bank) from %s", args["bank_ckpt"])
    model, trained_threshold = load_model_and_threshold(args["bank_ckpt"])
    threshold = args["threshold"] or trained_threshold
    logger.info("using threshold=%.4f (trained=%.4f)", threshold, trained_threshold)

    logger.info("loading FAISS index from %s", args["faiss_index"])
    faiss_index = faiss.read_index(args["faiss_index"])
    logger.info("FAISS index loaded: %d vectors", faiss_index.ntotal)

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
