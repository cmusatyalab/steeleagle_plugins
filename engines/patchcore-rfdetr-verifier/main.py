"""Entrypoint of the verifier plugin, which eagled starts with `pixi run start`.

eagled names the plugin to run in STEELEAGLE_PLUGIN, its args in
STEELEAGLE_PLUGIN_ARGS (a JSON object), and the name to serve the engine as,
already qualified with the local daemon's IP, in STEELEAGLE_ENGINE_ENDPOINT.
This plugin has no remote engines to watch, so STEELEAGLE_REMOTE_QUALIFIER is
unused.
"""
import argparse
import json
import logging
import os
import sys

from verifier import VARIANT_MAP, run_engine

VERIFIER_PLUGIN = "verifier.plugin.toml"

# Defaults of the args of verifier.plugin.toml. The daemon passes only the
# args set in its config, so any arg left unset is defaulted here.
DEFAULTS = {
    VERIFIER_PLUGIN: {
        "variant": "small",
        "confidence": 0.70,
        "model_path": "",
        "show_window": True,
        "max_output_size": 2_000_000,
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


def parse_cli_overrides():
    """Manual-run convenience, not part of eagled's contract: eagled only
    ever sets args through STEELEAGLE_PLUGIN_ARGS, never CLI flags, so these
    stay unset (None) and change nothing unless passed by hand, e.g.
    `python main.py --size small`."""
    parser = argparse.ArgumentParser(
        description="Optional overrides for a manual run; eagled sets these via STEELEAGLE_PLUGIN_ARGS instead"
    )
    parser.add_argument("--size", dest="variant", choices=sorted(VARIANT_MAP), default=None,
                         help="RF-DETR variant to run (overrides the variant arg)")
    known, _unused = parser.parse_known_args()
    return {name: value for name, value in vars(known).items() if value is not None}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    plugin = os.environ.get("STEELEAGLE_PLUGIN", "")
    if plugin not in DEFAULTS:
        sys.exit(f"Unrecognized plugin {plugin!r}; expected {VERIFIER_PLUGIN}")
    name = os.environ.get("STEELEAGLE_ENGINE_ENDPOINT")
    if not name:
        sys.exit("STEELEAGLE_ENGINE_ENDPOINT is not set")
    args = parse_args(plugin, os.environ.get("STEELEAGLE_PLUGIN_ARGS"))
    args.update(parse_cli_overrides())

    run_engine(name, args)


if __name__ == "__main__":
    main()
