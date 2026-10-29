"""Entrypoint of the h264 plugins, which eagled starts with `pixi run start`.

eagled names the plugin to run in STEELEAGLE_PLUGIN, its args in
STEELEAGLE_PLUGIN_ARGS (a JSON object), the name to serve the engine as,
already qualified with the local daemon's IP, in STEELEAGLE_ENGINE_ENDPOINT,
and the remote daemon's IP, if there is one, in STEELEAGLE_REMOTE_QUALIFIER.
"""
import json
import logging
import os
import sys

from h264.common import MAX_FRAME_SIZE
from h264.encode_decode import decode, encode

ENCODER_PLUGIN = "encoder.plugin.toml"
ENCODER_SYSTEM_PLUGIN = "encoder-system.plugin.toml"
DECODER_PLUGIN = "decoder.plugin.toml"
DECODER_SYSTEM_PLUGIN = "decoder-system.plugin.toml"

# Defaults of the args of each plugin, matching its plugin.toml. The daemon
# passes only the args set in its config, so any arg left unset is defaulted
# here.
DEFAULTS = {
    ENCODER_PLUGIN: {
        "decoder_name": "h264_decoder",
        "encoder": "x264enc tune=zerolatency speed-preset=ultrafast pass=quant quantizer=18 key-int-max=300",
        "fps": 30,
        "max_output_size": MAX_FRAME_SIZE,
        "timeout_ms": 1000,
        "log_frames": False,
        "gstreamer_path": "/usr/lib/gstreamer-1.0",
        "lib_path": "/usr/lib",
    },
    DECODER_PLUGIN: {
        "decoder": "avdec_h264 max-threads=1",
        "max_output_size": MAX_FRAME_SIZE,
        "timeout_ms": 1000,
        "log_frames": False,
        "gstreamer_path": "/usr/lib/gstreamer-1.0",
        "lib_path": "/usr/lib",
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

    # Overwrite environment variables for system use
    if plugin == ENCODER_SYSTEM_PLUGIN or plugin == DECODER_SYSTEM_PLUGIN:
        os.environ['GST_PLUGIN_SYSTEM_PATH'] = args['gstreamer_path']
        os.environ['LD_LIBRARY_PATH'] = args['lib_path']

    if plugin == ENCODER_PLUGIN or plugin == ENCODER_SYSTEM_PLUGIN:
        encode(name, args, remote_qualifier=os.environ.get("STEELEAGLE_REMOTE_QUALIFIER", ""))
    else:
        decode(name, args)


if __name__ == "__main__":
    main()
