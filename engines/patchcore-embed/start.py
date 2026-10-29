#!/usr/bin/env python3
"""Translates eagled's plugin env vars into inference-server-uriel's CLI."""
import json
import os
import sys

DEFAULTS = {
    "model": "models/libpatchcore_backbone_quant.so",
    "backend": "libs/libQnnHtp.so",
    "log_level": "info",
    "max_output_size": 8388608,
}

args = json.loads(os.environ.get("STEELEAGLE_PLUGIN_ARGS") or "{}")
for k, v in DEFAULTS.items():
    args.setdefault(k, v)

name = os.environ.get("STEELEAGLE_ENGINE_ENDPOINT")
if not name:
    sys.exit("STEELEAGLE_ENGINE_ENDPOINT is not set")

plugin_dir = os.path.dirname(os.path.abspath(__file__))


def resolve(path):
    return path if os.path.isabs(path) else os.path.join(plugin_dir, path)


# Prepend rather than replace: ADSP_LIBRARY_PATH overrides the DSP's default
# search path, so the system skel dirs must stay on it (separator is ';').
os.environ["ADSP_LIBRARY_PATH"] = ";".join(
    [os.path.join(plugin_dir, "libs"), os.environ.get("ADSP_LIBRARY_PATH", ""),
     "/usr/lib/rfsa/adsp", "/usr/lib/dsp/cdsp", "/dsp"]
)

os.execvp(
    os.path.join(plugin_dir, "inference-server-uriel"),
    [
        "inference-server-uriel",
        "--model", resolve(args["model"]),
        "--backend", resolve(args["backend"]),
        "--uriel_engine", name,
        "--uriel_task", "embed",
        "--log_level", str(args["log_level"]),
        "--uriel_max_result_size", str(args["max_output_size"]),
    ],
)
