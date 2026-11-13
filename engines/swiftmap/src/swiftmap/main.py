"""Entrypoint of the swiftmap plugins, which eagled starts with `pixi run start`.

eagled names the plugin to run in STEELEAGLE_PLUGIN, its args in
STEELEAGLE_PLUGIN_ARGS (a JSON object), and the name to serve the engine as in
STEELEAGLE_ENGINE_ENDPOINT. Reachback also uses STEELEAGLE_REMOTE_QUALIFIER.
"""

import json
import logging
import os
import signal
import sys
import tempfile
import threading
from pathlib import Path

from swiftmap import engine, reachback
from swiftmap.client import DEFAULT_PORT, SwiftMapClient
from swiftmap.early_discard import KeyframeFilter
from swiftmap.engine import Sender, SwiftMap
from swiftmap.keyframe import DEFAULT_MIN_DISPARITY
from swiftmap.reachback import FrameStore, Reachback

SWIFTMAP_PLUGIN = "swiftmap.plugin.toml"
REACHBACK_PLUGIN = "reachback.plugin.toml"

# Defaults of the args of each plugin, matching its plugin.toml. The daemon
# passes only the args set in its config, so any arg left unset is defaulted
# here.
DEFAULTS = {
    SWIFTMAP_PLUGIN: {
        "server": "localhost",
        "server_port": DEFAULT_PORT,
        "jpeg_quality": 95,
        "max_output_size": 1 << 20,
    },
    REACHBACK_PLUGIN: {
        "remote": "swiftmap",
        "min_disparity": DEFAULT_MIN_DISPARITY,
        "send_distance": 5.0,
        "max_output_size": 6237184,
    },
}
REQUIRED = {
    SWIFTMAP_PLUGIN: ("server",),
    REACHBACK_PLUGIN: ("remote",),
}


class ConfigError(Exception):
    pass


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
        if not str(args[name]).strip():
            raise ConfigError(f"the {name!r} arg must be set")
    args["max_output_size"] = int(args["max_output_size"])
    if args["max_output_size"] <= 0:
        raise ConfigError("max_output_size must be positive")
    if plugin == REACHBACK_PLUGIN:
        for name in ("min_disparity", "send_distance"):
            args[name] = float(args[name])
            if args[name] < 0:
                raise ConfigError(f"{name} must not be negative")
    if plugin == SWIFTMAP_PLUGIN:
        args["server_port"] = int(args["server_port"])
        args["jpeg_quality"] = int(args["jpeg_quality"])
        if not 1 <= args["jpeg_quality"] <= 100:
            raise ConfigError("jpeg_quality must be from 1 to 100")
    return args


def run_reachback(name, args, stop):
    remote_ip = os.environ.get("STEELEAGLE_REMOTE_QUALIFIER")
    if not remote_ip:
        sys.exit("reachback needs STEELEAGLE_REMOTE_QUALIFIER")
    # One database per service: a restart replaces it, however the last run ended
    path = Path(tempfile.gettempdir()) / f"swiftmap-reachback-{name}.sqlite3"
    path.unlink(missing_ok=True)
    store = FrameStore(str(path))
    keyframes = KeyframeFilter(min_disparity=args["min_disparity"],
                               send_distance=args["send_distance"])
    try:
        reachback.run(name, Reachback(store, keyframes, max_size=args["max_output_size"]),
                      f"{remote_ip}.{args['remote']}",
                      max_output_size=args["max_output_size"], stop=stop)
    finally:
        store.close()
        path.unlink(missing_ok=True)


def run_swiftmap(name, args, stop):
    client = SwiftMapClient(args["server"], args["server_port"])
    # The engine comes up without the server, and connects on the first frame
    # it sends
    if not client.connect(max_retries=1):
        logging.getLogger("swiftmap").warning(
            "SwiftMap server at %s:%s is not reachable yet; connecting on the first frame",
            args["server"], args["server_port"],
        )
    with Sender(client) as sender:
        swiftmap = SwiftMap(sender, jpeg_quality=args["jpeg_quality"])
        engine.run(name, swiftmap, max_output_size=args["max_output_size"], stop=stop)


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    plugin = os.environ.get("STEELEAGLE_PLUGIN", SWIFTMAP_PLUGIN)
    if plugin not in DEFAULTS:
        sys.exit(f"Unrecognized plugin {plugin!r}; expected one of {sorted(DEFAULTS)}")
    name = os.environ.get("STEELEAGLE_ENGINE_ENDPOINT")
    if not name:
        sys.exit("STEELEAGLE_ENGINE_ENDPOINT is not set")
    try:
        args = parse_args(plugin, os.environ.get("STEELEAGLE_PLUGIN_ARGS"))
    except (ConfigError, ValueError) as err:
        sys.exit(f"Bad args for {plugin}: {err}")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    if plugin == REACHBACK_PLUGIN:
        run_reachback(name, args, stop)
    else:
        run_swiftmap(name, args, stop)


if __name__ == "__main__":
    main()
