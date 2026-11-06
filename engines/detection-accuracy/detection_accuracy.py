"""Uriel engine: tracks precision, recall, and "effective recall" of a
detection engine's forwarded frames against a ground-truth rubric loaded
once at startup. Runs no inference itself -- it only reads the frame id out
of each forwarded RawFrame and checks it against the rubric's
(label, first_frame, last_frame) appearances. A frame reaching this engine
at all means the upstream detection engine already called it a positive.

Normally a terminal sink (drops every input after accounting for it) and
reports via periodic log lines. The latest running snapshot is also
written to STATS_PATH on every frame; an independent `sleep && cat`
subprocess reads that file and appends it to DEADLINE_LOG_PATH at
rubric["duration_seconds"] + emit_warmup_seconds after startup -- a
separate process, not a Python thread, so it fires on schedule even if
frames stop arriving before the deadline, and it writes to its own file
rather than this process's stdout since that didn't reliably survive
pixi/uv's output capture when tested. Set forward_frames to also pass each
frame on unchanged (e.g. to view in GCS), tagged with the latest snapshot.
"""
import json
import logging
import subprocess

import uriel
from google.protobuf.message import DecodeError

from steeleagle_protocol.v1.messages.frame import frame_pb2

logger = logging.getLogger("detection-accuracy")

STATS_PATH = "/tmp/detection_accuracy_stats.json"
DEADLINE_LOG_PATH = "/tmp/detection_accuracy_deadline.log"


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


def write_stats_file(stats):
    with open(STATS_PATH, "w") as f:
        json.dump(stats, f)


def run_engine(name, args):
    """Runs the accuracy tracker as the engine `name`. `args` has every arg
    of detection_accuracy.plugin.toml."""
    rubric = load_rubric(args["rubric_path"])
    appearances = rubric["appearances"]
    gt_frame_count = ground_truth_frame_count(appearances)
    video_done_at = rubric["duration_seconds"] + args["emit_warmup_seconds"]

    subprocess.Popen(["sh", "-c",
        f"sleep {int(round(video_done_at))} && "
        f"(echo 'VIDEO SHOULD BE DONE (deadline reached):'; cat {STATS_PATH}) >> {DEADLINE_LOG_PATH} 2>&1"])

    forwarded = 0
    true_positives = 0
    since_last_frame_emit = 0

    with uriel.Engine(name, max_output_size=args["max_output_size"]) as engine:
        logger.info("engine %s ready; rubric has %d appearances, %d ground-truth frames; "
                    "expecting video done at %.1fs (see %s)", name, len(appearances), gt_frame_count,
                    video_done_at, DEADLINE_LOG_PATH)
        for input in engine:
            encoded_frame = frame_pb2.EncodedFrame()
            try:
                encoded_frame.ParseFromString(bytes(input.data))
            except DecodeError as e:
                logger.warning("input %d: not a EncodedFrame (%s); dropping", input.id.seq, e)
                engine.drop()
                continue

            forwarded += 1
            since_last_frame_emit += 1
            appearance = find_appearance(appearances, encoded_frame.id)
            if appearance is not None:
                true_positives += 1
                appearance["hit"] = True

            stats = snapshot(forwarded, true_positives, appearances, gt_frame_count)
            write_stats_file(stats)

            if args["emit_every_frames"] and since_last_frame_emit >= args["emit_every_frames"]:
                since_last_frame_emit = 0
                logger.info("snapshot: %s", json.dumps(stats))

            if args["forward_frames"]:
                engine.forward(result=json.dumps(stats).encode())
            else:
                engine.drop()
