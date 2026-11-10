"""Entrypoint of the detection-log plugin, which eagled starts with
`pixi run start`.

eagled names the plugin to run in STEELEAGLE_PLUGIN, its args in
STEELEAGLE_PLUGIN_ARGS (a JSON object), the name to serve the engine as in
STEELEAGLE_ENGINE_ENDPOINT, the IP of the local daemon, which qualifies the
names of its engines, in STEELEAGLE_LOCAL_QUALIFIER, and the directory
transferred files are saved in in STEELEAGLE_FILES.
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from detection_log.engine import run
from detection_log.frames import FrameSaver
from detection_log.log import DetectionLog

PLUGIN_FILE = "detection-log.plugin.toml"
FILES_ENV = "STEELEAGLE_FILES"
# Where eagled mounts the files directory in a sandbox, where the host path in
# STEELEAGLE_FILES may not exist.
SANDBOX_FILES_DIR = Path("/steeleagle/files")
# Defaults of the args, matching the plugin.toml. The daemon passes only the
# args set in its config, so any arg left unset is defaulted here.
DEFAULTS = {"detector": "", "timeout": 2000}


class ConfigError(Exception):
    pass


def parse_args(content):
    """The args in the JSON object `content`, with every unset arg
    defaulted."""
    args = json.loads(content or "{}")
    if not isinstance(args, dict):
        raise ConfigError("the args must be a JSON object")
    for name, default in DEFAULTS.items():
        if args.get(name) is None:
            args[name] = default
    args["detector"] = str(args["detector"]).strip()
    if not args["detector"]:
        raise ConfigError("the 'detector' arg must be set")
    timeout = args["timeout"]
    if isinstance(timeout, bool) or not float(timeout).is_integer():
        raise ConfigError(f"timeout must be a whole number of milliseconds, not {timeout!r}")
    args["timeout"] = int(timeout)
    if args["timeout"] <= 0:
        raise ConfigError("timeout must be positive")
    return args


def qualify(qualifier, name):
    """`name` qualified with a daemon's IP, as eagled names its services, or
    as is if there is no qualifier."""
    return f"{qualifier}.{name}" if qualifier else name


def engine_dir_name(endpoint):
    """The name the plugin runs as: the last '.'-separated part of its
    STEELEAGLE_ENGINE_ENDPOINT."""
    return endpoint.rsplit(".", 1)[-1]


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
    except (ConfigError, ValueError, TypeError) as err:
        sys.exit(f"Bad args for {PLUGIN_FILE}: {err}")
    detector = qualify(os.environ.get("STEELEAGLE_LOCAL_QUALIFIER", ""), args["detector"])

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log = DetectionLog(
        detector=detector,
        saver=FrameSaver(directory),
        timeout_ns=args["timeout"] * 1_000_000,
        clock=time.monotonic_ns,
    )
    run(name, log, stop=stop)


if __name__ == "__main__":
    main()
