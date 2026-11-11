"""Entrypoint of the led-clock plugin, which eagled starts with
`pixi run start`.

eagled names the plugin to run in STEELEAGLE_PLUGIN, its args in
STEELEAGLE_PLUGIN_ARGS (a JSON object), the name to serve the engine as in
STEELEAGLE_ENGINE_ENDPOINT, and the directory transferred files are saved in in
STEELEAGLE_FILES.
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from led_clock.decode import LedReader
from led_clock.engine import run
from led_clock.latency import NANOS_PER_MILLI, NANOS_PER_SECOND, LatencyLog
from led_clock.snapshot import JpegSaver

PLUGIN_FILE = "led-clock.plugin.toml"
FILES_ENV = "STEELEAGLE_FILES"
# Where eagled mounts the files directory in a sandbox, where the host path in
# STEELEAGLE_FILES may not exist.
SANDBOX_FILES_DIR = Path("/steeleagle/files")
# Defaults of the args, matching the plugin.toml. The daemon passes only the
# args set in its config, so any arg left unset is defaulted here.
DEFAULTS = {
    "tick_ms": 5,
    "fps": 30,
    "report_interval_seconds": 5,
    "snapshot_interval_seconds": 5,
}


class ConfigError(Exception):
    pass


def parse_args(content):
    """The args in the JSON object `content`, with every unset arg
    defaulted. Every arg is a positive number."""
    args = json.loads(content or "{}")
    if not isinstance(args, dict):
        raise ConfigError("the args must be a JSON object")
    for name, default in DEFAULTS.items():
        value = args.get(name)
        if value is None:
            args[name] = default
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ConfigError(f"{name} must be a positive number, not {value!r}")
    return args


def engine_dir_name(endpoint):
    """The directory snapshots are saved under: the plugin's whole qualified
    STEELEAGLE_ENGINE_ENDPOINT, which must name a directory inside the files
    directory."""
    if not endpoint or endpoint in (".", "..") or Path(endpoint).name != endpoint:
        raise ConfigError(f"{endpoint!r} is not a name to save snapshots under")
    return endpoint


def files_dir(env):
    """The directory transferred files are saved in."""
    configured = env.get(FILES_ENV)
    if configured and Path(configured).is_dir():
        return Path(configured)
    if SANDBOX_FILES_DIR.is_dir():
        return SANDBOX_FILES_DIR
    raise ConfigError(f"{FILES_ENV} is unset or missing, and {SANDBOX_FILES_DIR} does not exist")


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
        directory = files_dir(os.environ) / engine_dir_name(name)
    except (ConfigError, ValueError) as err:
        sys.exit(f"Bad args for {PLUGIN_FILE}: {err}")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log = LatencyLog(
        tick_ns=round(args["tick_ms"] * NANOS_PER_MILLI),
        fps=args["fps"],
        reader=LedReader(),
        saver=JpegSaver(directory),
        clock=time.monotonic_ns,
        report_interval_ns=round(args["report_interval_seconds"] * NANOS_PER_SECOND),
        snapshot_interval_ns=round(args["snapshot_interval_seconds"] * NANOS_PER_SECOND),
    )
    run(name, log, stop=stop)


if __name__ == "__main__":
    main()
