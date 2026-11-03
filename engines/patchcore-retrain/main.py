"""
Entrypoint of the patchcore-retrain plugin, which eagled starts with
`pixi run start`.

eagled names the plugin to run in STEELEAGLE_PLUGIN, its args in
STEELEAGLE_PLUGIN_ARGS (a JSON object), and the name to serve the engine as,
already qualified with the local daemon's IP, in STEELEAGLE_ENGINE_ENDPOINT.
This plugin has no remote engines to watch itself (patchcore-score is the
one that subscribes to it, not the other way around), so
STEELEAGLE_REMOTE_QUALIFIER is unused.
"""
import json
import logging
import os
import sys

from retrain import run_engine

RETRAIN_PLUGIN = "patchcore-retrain.plugin.toml"

# Defaults of the args of patchcore-retrain.plugin.toml. The daemon passes
# only the args set in its config, so any arg left unset is defaulted here.
DEFAULTS = {
    RETRAIN_PLUGIN: {
        "daemon_addr": "fermi.local:50051",
        "n_max": 20,
        "m_min": 8,
        "fp_rate_trigger": 0.8,
        "window_stale_seconds": 60.0,
        "tolerance_fraction": 0.05,
        "retrain_collect_seconds": 30.0,
        "default_bank_size": 1000,
        "max_output_size": 4096,
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
        sys.exit(f"Unrecognized plugin {plugin!r}; expected {RETRAIN_PLUGIN}")
    name = os.environ.get("STEELEAGLE_ENGINE_ENDPOINT")
    if not name:
        sys.exit("STEELEAGLE_ENGINE_ENDPOINT is not set")
    args = parse_args(plugin, os.environ.get("STEELEAGLE_PLUGIN_ARGS"))

    run_engine(name, args)


if __name__ == "__main__":
    main()
