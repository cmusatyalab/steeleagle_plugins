"""Uriel engine: tracks precision, recall, and "effective recall" of a
detection engine's forwarded frames against a ground-truth rubric loaded
once at startup. Runs no inference itself -- it only reads the frame id out
of each forwarded RawFrame and checks it against the rubric's
(label, first_frame, last_frame) appearances. A frame reaching this engine
at all means the upstream detection engine already called it a positive.

Normally a terminal sink (drops every input after accounting for it) and
reports via periodic log lines, plus a single, distinctly-labeled
"video should be done" line once rubric["duration_seconds"] +
emit_warmup_seconds have elapsed since this engine started -- that line
fires exactly once, since the source video is a single finite pass, not a
loop. Set forward_frames to also pass each frame on unchanged (e.g. to view
in GCS), tagged with the latest running snapshot.
"""
import json
import logging
import time

import uriel
from google.protobuf.message import DecodeError

from steeleagle_protocol.v1.messages.frame import frame_pb2

logger = logging.getLogger("detection-accuracy")


def load_rubric(path):
    with open(path) as f:
        rubric = json.load(f)
    for appearance in rubric["appearances"]:
        appearance["hit"] = False
    return rubric


def find_appearance(appearances, frame_id):
    """Returns the appearance dict `frame_id` falls inside, or None."""
    for appearance in appearances:
        if appearance["first_frame"] <= frame_id <= appearance["last_frame"]:
            return appearance
    return None


def ground_truth_frame_count(appearances):
    return sum(a["last_frame"] - a["first_frame"] + 1 for a in appearances)


def snapshot(forwarded, true_positives, appearances, gt_frame_count):
    hits = sum(1 for a in appearances if a["hit"])
    return {
        "forwarded": forwarded,
        "true_positive_frames": true_positives,
        "false_positive_frames": forwarded - true_positives,
        "precision": true_positives / forwarded if forwarded else 0.0,
        "recall": true_positives / gt_frame_count if gt_frame_count else 0.0,
        "effective_recall": hits / len(appearances) if appearances else 0.0,
        "appearances_hit": hits,
        "appearances_total": len(appearances),
    }


def run_engine(name, args):
    """Runs the accuracy tracker as the engine `name`. `args` has every arg
    of detection_accuracy.plugin.toml."""
    rubric = load_rubric(args["rubric_path"])
    appearances = rubric["appearances"]
    gt_frame_count = ground_truth_frame_count(appearances)
    video_done_at = rubric["duration_seconds"] + args["emit_warmup_seconds"]

    forwarded = 0
    true_positives = 0
    since_last_frame_emit = 0
    video_done_logged = False
    start_time = time.monotonic()

    with uriel.Engine(name, max_output_size=args["max_output_size"]) as engine:
        logger.info("engine %s ready; rubric has %d appearances, %d ground-truth frames; "
                    "expecting video done at %.1fs", name, len(appearances), gt_frame_count,
                    video_done_at)
        for input in engine:
            raw_frame = frame_pb2.RawFrame()
            try:
                raw_frame.ParseFromString(bytes(input.data))
            except DecodeError as e:
                logger.warning("input %d: not a RawFrame (%s); dropping", input.id.seq, e)
                engine.drop()
                continue

            forwarded += 1
            since_last_frame_emit += 1
            appearance = find_appearance(appearances, raw_frame.id)
            if appearance is not None:
                true_positives += 1
                appearance["hit"] = True

            stats = snapshot(forwarded, true_positives, appearances, gt_frame_count)
            elapsed = time.monotonic() - start_time

            if args["emit_every_frames"] and since_last_frame_emit >= args["emit_every_frames"]:
                since_last_frame_emit = 0
                logger.info("snapshot: %s", json.dumps(stats))

            if not video_done_logged and elapsed >= video_done_at:
                video_done_logged = True
                logger.info("VIDEO SHOULD BE DONE (elapsed %.1fs): %s", elapsed, json.dumps(stats))

            if args["forward_frames"]:
                engine.forward(result=json.dumps(stats).encode())
            else:
                engine.drop()
