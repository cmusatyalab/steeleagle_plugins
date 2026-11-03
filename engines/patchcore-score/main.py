"""Entrypoint of the patchcore-score plugin, which eagled starts with
`pixi run start`.

eagled names the plugin to run in STEELEAGLE_PLUGIN, its args in
STEELEAGLE_PLUGIN_ARGS (a JSON object), and the name to serve the engine as,
already qualified with the local daemon's IP, in STEELEAGLE_ENGINE_ENDPOINT.

STEELEAGLE_REMOTE_QUALIFIER is patchcore-retrain's qualified engine name --
this plugin subscribes to it (same contract h264_encoder uses to watch
h264_decoder) so it can hot-swap its FAISS bank when retrain announces one,
without a restart.
"""
import json
import logging
import os
import sys

from score import run_engine

SCORE_PLUGIN = "score.plugin.toml"

# Defaults of the args of score.plugin.toml. The daemon passes only the args
# set in its config, so any arg left unset is defaulted here.
DEFAULTS = {
    SCORE_PLUGIN: {
        "threshold_meta": "",
        "faiss_index": "",
        "threshold": 0,
        "max_output_size": 3_145_728,
    },
}


def parse_args(plugin, content):
    """The args in the JSON object `content`, with every unset arg of
    `plugin` defaulted."""
    args = json.loads(content or "{}")
    for name, default in DEFAULTS[plugin].items():
        if args.get(name) is None:
            args[name] = default
    return args


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    plugin = os.environ.get("STEELEAGLE_PLUGIN", "")
    if plugin not in DEFAULTS:
        sys.exit(f"Unrecognized plugin {plugin!r}; expected {SCORE_PLUGIN}")
    name = os.environ.get("STEELEAGLE_ENGINE_ENDPOINT")
    if not name:
        sys.exit("STEELEAGLE_ENGINE_ENDPOINT is not set")
    remote_qualifier = os.environ.get("STEELEAGLE_REMOTE_QUALIFIER")
    args = parse_args(plugin, os.environ.get("STEELEAGLE_PLUGIN_ARGS"))

    if not args["threshold_meta"] or not args["faiss_index"]:
        sys.exit("args 'threshold_meta' and 'faiss_index' are required (set them in STEELEAGLE_PLUGIN_ARGS)")

    run_engine(name, args, remote_qualifier)


if __name__ == "__main__":
    main()
