"""Entrypoint of the thumbnail plugins, which eagled starts with `pixi run start`.

eagled names the plugin to run in STEELEAGLE_PLUGIN, its args in
STEELEAGLE_PLUGIN_ARGS (a JSON object), and the name to serve the engine as,
already qualified with the local daemon's IP, in STEELEAGLE_ENGINE_ENDPOINT.
"""
import json
import logging
import os
import sys

from thumbnail.frames import MAX_FRAME_SIZE

JPEG_ENCODER_PLUGIN = "jpeg_encoder.plugin.toml"
JPEG_DECODER_PLUGIN = "jpeg_decoder.plugin.toml"
WEBP_ENCODER_PLUGIN = "webp_encoder.plugin.toml"
WEBP_DECODER_PLUGIN = "webp_decoder.plugin.toml"

# Defaults of the args of each plugin, matching its plugin.toml. The daemon
# passes only the args set in its config, so any arg left unset is defaulted
# here.
DEFAULTS = {
    JPEG_ENCODER_PLUGIN: {
        "encoder": "jpegenc quality=85",
        "max_output_size": MAX_FRAME_SIZE,
        "timeout_ms": 1000,
        "log_frames": False,
    },
    JPEG_DECODER_PLUGIN: {
        "decoder": "jpegdec",
        "max_output_size": MAX_FRAME_SIZE,
        "timeout_ms": 1000,
        "log_frames": False,
    },
    WEBP_ENCODER_PLUGIN: {
        "quality": 60,
        "method": 4,
        "lossless": False,
        "max_output_size": MAX_FRAME_SIZE,
        "log_frames": False,
    },
    WEBP_DECODER_PLUGIN: {
        "max_output_size": MAX_FRAME_SIZE,
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


def runner(plugin):
    """The function that runs `plugin`. Each codec is imported only when it
    runs, so the WebP engines never load GStreamer."""
    if plugin in (JPEG_ENCODER_PLUGIN, JPEG_DECODER_PLUGIN):
        from thumbnail import jpeg as codec
    else:
        from thumbnail import webp as codec
    return codec.encode if plugin in (JPEG_ENCODER_PLUGIN, WEBP_ENCODER_PLUGIN) else codec.decode


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    plugin = os.environ.get("STEELEAGLE_PLUGIN", "")
    if plugin not in DEFAULTS:
        sys.exit(f"Unrecognized plugin {plugin!r}; expected one of {', '.join(DEFAULTS)}")
    name = os.environ.get("STEELEAGLE_ENGINE_ENDPOINT")
    if not name:
        sys.exit("STEELEAGLE_ENGINE_ENDPOINT is not set")
    args = parse_args(plugin, os.environ.get("STEELEAGLE_PLUGIN_ARGS"))
    runner(plugin)(name, args)


if __name__ == "__main__":
    main()
