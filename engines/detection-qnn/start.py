#!/usr/bin/env python3
"""Translates eagled's plugin env vars into inference-server-uriel's CLI."""
import json
import os
import sys

DEFAULTS = {
    "model": "libs640_v26_quant.so",
    "labels": "",
    "backend": "libs/libQnnHtp.so",
    "only_classes": [],
    "confidence": 0.5,
    "nms_score_threshold": 0.5,
    "nms_iou_threshold": 0.3,
    "log_level": "info",
    "max_output_size": 65536,
    "early_discard": False,
}

args = json.loads(os.environ.get("STEELEAGLE_PLUGIN_ARGS") or "{}")
for k, v in DEFAULTS.items():
    args.setdefault(k, v)

name = os.environ.get("STEELEAGLE_ENGINE_ENDPOINT")
if not name:
    sys.exit("STEELEAGLE_ENGINE_ENDPOINT is not set")

plugin_dir = os.path.dirname(os.path.abspath(__file__))

MODELS_ENV = "STEELEAGLE_MODELS"
SANDBOX_MODELS_DIR = "/steeleagle/models"
MODEL_SUFFIX = ".so"
LABELS_SUFFIX = ".labels"
CLASS_SEPARATOR = ","  # of --only_classes


def resolve(path):
    return path if os.path.isabs(path) else os.path.join(plugin_dir, path)


# eagled sets STEELEAGLE_MODELS to the directory transferred models are saved
# in, and mounts it at /steeleagle/models in a sandbox, where the host path may
# not exist.
models_dir = os.environ.get(MODELS_ENV)
if not models_dir or not os.path.isdir(models_dir):
    models_dir = SANDBOX_MODELS_DIR

model_name = args["model"]
if os.path.basename(model_name) != model_name or not model_name.endswith(MODEL_SUFFIX):
    sys.exit(f"model must be a {MODEL_SUFFIX} file name without directories: {model_name!r}")

model = os.path.join(models_dir, model_name)
if not os.path.isfile(model):
    sys.exit(f"Compiled model not found: {model}")

# Class names come from an explicit labels file, else from a file named after
# the model (libfoo.so -> libfoo.labels) transferred alongside it. With neither,
# the server falls back to the COCO classes.
labels_name = args["labels"]
if labels_name:
    if os.path.basename(labels_name) != labels_name:
        sys.exit(f"labels must be a file name without directories: {labels_name!r}")
    labels = os.path.join(models_dir, labels_name)
    if not os.path.isfile(labels):
        sys.exit(f"Labels file not found: {labels}")
else:
    labels = os.path.join(models_dir, os.path.splitext(model_name)[0] + LABELS_SUFFIX)
    if not os.path.isfile(labels):
        labels = None

# Prepend rather than replace: ADSP_LIBRARY_PATH overrides the DSP's default
# search path, so the system skel dirs must stay on it (separator is ';').
os.environ["ADSP_LIBRARY_PATH"] = ";".join(
    [os.path.join(plugin_dir, "libs"), os.environ.get("ADSP_LIBRARY_PATH", ""),
     "/usr/lib/rfsa/adsp", "/usr/lib/dsp/cdsp", "/dsp"]
)

server_args = [
    "inference-server-uriel",
    "--model", model,
    "--backend", resolve(args["backend"]),
    "--uriel_engine", name,
    "--uriel_task", "detect",
    # Forward every frame, or only the frames with detections
    "--uriel_forward", "detections" if args["early_discard"] else "always",
    "--confidence_threshold", str(args["confidence"]),
    "--nms_score_threshold", str(args["nms_score_threshold"]),
    "--nms_iou_threshold", str(args["nms_iou_threshold"]),
    "--log_level", str(args["log_level"]),
    "--uriel_max_result_size", str(args["max_output_size"]),
]
if labels:
    server_args += ["--labels", labels]
if args["only_classes"]:
    server_args += ["--only_classes", CLASS_SEPARATOR.join(args["only_classes"])]

os.execvp(os.path.join(plugin_dir, "inference-server-uriel"), server_args)
