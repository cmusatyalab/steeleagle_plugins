"""Uriel engine: scores FrameWithEmbedding (from the C++ embed engine) against
the PatchCore memory bank via FAISS, forwards the frame if anomalous, drops it
otherwise. Scoring logic ported verbatim from patchcore_csv_faiss.py.
"""
import logging
import time

import faiss
import numpy as np
import torch
import uriel
from anomalib.models import Patchcore

from steeleagle_protocol.v1.messages.result import result_pb2

logger = logging.getLogger("patchcore-score")


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
            combined = result_pb2.FrameWithEmbedding()
            combined.ParseFromString(bytes(input.data))

            try:
                anomaly_score = score(model, faiss_index, combined.embedding)
            except ValueError as e:
                logger.error("bad embedding, dropping: %s", e)
                engine.drop()
                continue

            is_anomalous = anomaly_score > threshold
            elapsed_ms = (time.perf_counter() - t0) * 1000
            result = f'{{"score": {anomaly_score:.4f}, "anomalous": {is_anomalous}}}'.encode()

            if is_anomalous:
                frame_bytes = combined.encoded_frame
                buf = engine.loan()
                buf[: len(frame_bytes)] = frame_bytes
                engine.replace(len(frame_bytes), output_type="", result=result)
                logger.info("ANOMALOUS score=%.4f (%.1fms) -> forwarded", anomaly_score, elapsed_ms)
            else:
                engine.drop(result=result)
                logger.info("normal    score=%.4f (%.1fms) -> dropped", anomaly_score, elapsed_ms)
