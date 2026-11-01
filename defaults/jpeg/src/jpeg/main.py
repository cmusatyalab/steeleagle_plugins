"""Entrypoint of the jpeg plugins, which eagled starts with `pixi run start`.

eagled names the plugin to run in STEELEAGLE_PLUGIN, its args in
STEELEAGLE_PLUGIN_ARGS (a JSON object), and the name to serve the engine as,
already qualified with the local daemon's IP, in STEELEAGLE_ENGINE_ENDPOINT.
"""
import json
import logging
import os
import sys

from jpeg.common import MAX_FRAME_SIZE
from jpeg.encode_decode import decode, encode

ENCODER_PLUGIN = "encoder.plugin.toml"
DECODER_PLUGIN = "decoder.plugin.toml"

# Defaults of the args of each plugin, matching its plugin.toml. The daemon
# passes only the args set in its config, so any arg left unset is defaulted
# here.
DEFAULTS = {
    ENCODER_PLUGIN: {
        "encoder": "jpegenc quality=85",
        "max_output_size": MAX_FRAME_SIZE,
        "timeout_ms": 1000,
        "log_frames": False,
    },
    DECODER_PLUGIN: {
        "decoder": "jpegdec",
        "max_output_size": MAX_FRAME_SIZE,
        "timeout_ms": 1000,
        "log_frames": False,
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
        sys.exit(f"Unrecognized plugin {plugin!r}; expected {ENCODER_PLUGIN} or {DECODER_PLUGIN}")
    name = os.environ.get("STEELEAGLE_ENGINE_ENDPOINT")
    if not name:
        sys.exit("STEELEAGLE_ENGINE_ENDPOINT is not set")
    args = parse_args(plugin, os.environ.get("STEELEAGLE_PLUGIN_ARGS"))

    if plugin == ENCODER_PLUGIN:
        encode(name, args)
    else:
        decode(name, args)


if __name__ == "__main__":
    main()
