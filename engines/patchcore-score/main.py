"""Entrypoint of the patchcore-score plugin, which eagled starts with
`pixi run start`.

eagled names the plugin to run in STEELEAGLE_PLUGIN, its args in
STEELEAGLE_PLUGIN_ARGS (a JSON object), and the name to serve the engine as,
already qualified with the local daemon's IP, in STEELEAGLE_ENGINE_ENDPOINT.
This plugin has no remote engines to watch, so STEELEAGLE_REMOTE_QUALIFIER is
unused.
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
        "bank_ckpt": "",
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
    args = parse_args(plugin, os.environ.get("STEELEAGLE_PLUGIN_ARGS"))

    if not args["bank_ckpt"] or not args["faiss_index"]:
        sys.exit("args 'bank_ckpt' and 'faiss_index' are required (set them in STEELEAGLE_PLUGIN_ARGS)")

    run_engine(name, args)


if __name__ == "__main__":
    main()
