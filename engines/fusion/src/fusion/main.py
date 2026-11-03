"""Entrypoint of the fusion plugins, which eagled starts with `pixi run start`.

eagled names the plugin to run in STEELEAGLE_PLUGIN, its args in
STEELEAGLE_PLUGIN_ARGS (a JSON object), the name to serve the engine as in
STEELEAGLE_ENGINE_ENDPOINT, and the IPs of the local and remote daemons, which
qualify the names of their engines, in STEELEAGLE_LOCAL_QUALIFIER and
STEELEAGLE_REMOTE_QUALIFIER.
"""

import json
import logging
import os
import signal
import sys
import threading
import time

from fusion.detection import DetectionFusion
from fusion.engine import qualify, run

FUSION_DETECTION_PLUGIN = "fusion_detection.plugin.toml"
DETECTION_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/DetectionResult"
# The largest result the engine reports; a larger one is dropped.
MAX_RESULT_SIZE = 1 << 20

# Defaults of the args of each plugin, matching its plugin.toml. The daemon
# passes only the args set in its config, so any arg left unset is defaulted
# here.
DEFAULTS = {
    FUSION_DETECTION_PLUGIN: {"local": "", "remote": "", "grace": 200, "timeout": 1000},
}
REQUIRED = {
    FUSION_DETECTION_PLUGIN: ("local", "remote"),
}


class ConfigError(Exception):
    pass


def milliseconds(args, name):
    """The arg `name` as a whole number of milliseconds."""
    value = args[name]
    if isinstance(value, bool) or not float(value).is_integer():
        raise ConfigError(f"{name} must be a whole number of milliseconds, not {value!r}")
    return int(value)


def parse_args(plugin, content):
    """The args in the JSON object `content`, with every unset arg of
    `plugin` defaulted."""
    args = json.loads(content or "{}")
    if not isinstance(args, dict):
        raise ConfigError("the args must be a JSON object")
    for name, default in DEFAULTS[plugin].items():
        if args.get(name) is None:
            args[name] = default
    for name in REQUIRED[plugin]:
        args[name] = str(args[name]).strip()
        if not args[name]:
            raise ConfigError(f"the {name!r} arg must be set")
    args["grace"] = milliseconds(args, "grace")
    if args["grace"] < 0:
        raise ConfigError("grace must not be negative")
    args["timeout"] = milliseconds(args, "timeout")
    if args["timeout"] <= 0:
        raise ConfigError("timeout must be positive")
    return args


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    plugin = os.environ.get("STEELEAGLE_PLUGIN", "")
    if plugin not in DEFAULTS:
        sys.exit(f"Unrecognized plugin {plugin!r}; expected one of {sorted(DEFAULTS)}")
    name = os.environ.get("STEELEAGLE_ENGINE_ENDPOINT")
    if not name:
        sys.exit("STEELEAGLE_ENGINE_ENDPOINT is not set")
    try:
        args = parse_args(plugin, os.environ.get("STEELEAGLE_PLUGIN_ARGS"))
    except (ConfigError, ValueError, TypeError) as err:
        sys.exit(f"Bad args for {plugin}: {err}")
    local = qualify(os.environ.get("STEELEAGLE_LOCAL_QUALIFIER", ""), args["local"])
    remote = qualify(os.environ.get("STEELEAGLE_REMOTE_QUALIFIER", ""), args["remote"])

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    try:
        handler = DetectionFusion(
            local=local,
            remote=remote,
            grace_ns=args["grace"] * 1_000_000,
            timeout_ns=args["timeout"] * 1_000_000,
            clock=time.monotonic_ns,
        )
    except ValueError as err:
        sys.exit(f"Bad args for {plugin}: {err}")
    logging.getLogger("fusion").info(
        "preferring %s's result for %d ms after %s's; giving up on a frame after %d ms",
        remote,
        args["grace"],
        local,
        args["timeout"],
    )
    run(
        name,
        handler,
        [local, remote],
        result_type=DETECTION_RESULT_TYPE,
        max_output_size=MAX_RESULT_SIZE,
        stop=stop,
    )


if __name__ == "__main__":
    main()
