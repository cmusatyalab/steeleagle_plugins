"""Entrypoint of the detection-crop plugin, which eagled starts with
`pixi run start`.

eagled names the plugin to run in STEELEAGLE_PLUGIN, its args in
STEELEAGLE_PLUGIN_ARGS (a JSON object), the name to serve the engine as in
STEELEAGLE_ENGINE_ENDPOINT, and the IP of the local daemon, which qualifies
the names of its engines, in STEELEAGLE_LOCAL_QUALIFIER.
"""

import json
import logging
import os
import signal
import sys
import threading
import time

from detection_crop.crop import max_output_size, normalize_class
from detection_crop.cropper import Cropper
from detection_crop.engine import run

PLUGIN_FILE = "detection-crop.plugin.toml"
# Defaults of the args, matching the plugin.toml. The daemon passes only the
# args set in its config, so any arg left unset is defaulted here.
DEFAULTS = {"detector": "", "class": "", "timeout": 2000, "max_width": 1920, "max_height": 1080}


class ConfigError(Exception):
    pass


def _positive_int(args, name, unit):
    value = args[name]
    if isinstance(value, bool) or not float(value).is_integer() or value <= 0:
        raise ConfigError(f"{name} must be a positive whole number of {unit}, not {value!r}")
    return int(value)


def parse_args(content):
    """The args in the JSON object `content`, with every unset arg
    defaulted."""
    args = json.loads(content or "{}")
    if not isinstance(args, dict):
        raise ConfigError("the args must be a JSON object")
    for name, default in DEFAULTS.items():
        if args.get(name) is None:
            args[name] = default
    for name in ("detector", "class"):
        args[name] = str(args[name]).strip()
        if not args[name]:
            raise ConfigError(f"the {name!r} arg must be set")
    args["timeout"] = _positive_int(args, "timeout", "milliseconds")
    args["max_width"] = _positive_int(args, "max_width", "pixels")
    args["max_height"] = _positive_int(args, "max_height", "pixels")
    return args


def qualify(qualifier, name):
    """`name` qualified with a daemon's IP, as eagled names its services, or
    as is if there is no qualifier."""
    return f"{qualifier}.{name}" if qualifier else name


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    plugin = os.environ.get("STEELEAGLE_PLUGIN", "")
    if plugin != PLUGIN_FILE:
        sys.exit(f"Unrecognized plugin {plugin!r}; expected {PLUGIN_FILE}")
    name = os.environ.get("STEELEAGLE_ENGINE_ENDPOINT")
    if not name:
        sys.exit("STEELEAGLE_ENGINE_ENDPOINT is not set")
    try:
        args = parse_args(os.environ.get("STEELEAGLE_PLUGIN_ARGS"))
    except (ConfigError, ValueError, TypeError) as err:
        sys.exit(f"Bad args for {PLUGIN_FILE}: {err}")
    detector = qualify(os.environ.get("STEELEAGLE_LOCAL_QUALIFIER", ""), args["detector"])

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    cropper = Cropper(
        detector=detector,
        class_name=normalize_class(args["class"]),
        timeout_ns=args["timeout"] * 1_000_000,
        max_output_size=max_output_size(args["max_width"], args["max_height"]),
        clock=time.monotonic_ns,
    )
    run(name, cropper, stop=stop)


if __name__ == "__main__":
    main()
