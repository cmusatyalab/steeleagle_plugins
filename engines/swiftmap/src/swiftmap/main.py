"""Entrypoint of the swiftmap plugins (`pixi run start`). eagled names the plugin
in STEELEAGLE_PLUGIN, its args in STEELEAGLE_PLUGIN_ARGS (JSON), and the engine
name in STEELEAGLE_ENGINE_ENDPOINT; reachback also needs STEELEAGLE_REMOTE_QUALIFIER.
Unset args take the defaults in the plugin's .plugin.toml."""

import json
import logging
import os
import signal
import sys
import tempfile
import threading
import tomllib
from pathlib import Path

from swiftmap import onboard, reachback, remote

SWIFTMAP_PLUGIN = "swiftmap.plugin.toml"
REACHBACK_PLUGIN = "reachback.plugin.toml"
PREPROCESS_PLUGIN = "preprocess.plugin.toml"
PLUGINS = (SWIFTMAP_PLUGIN, REACHBACK_PLUGIN, PREPROCESS_PLUGIN)
PLUGIN_DIR = Path(__file__).resolve().parents[2]


class ConfigError(Exception):
    pass


def defaults(plugin):
    """{arg: default} from the plugin's .plugin.toml."""
    with open(PLUGIN_DIR / plugin, "rb") as f:
        spec = tomllib.load(f)
    return {a["name"]: a["default"] for a in spec.get("args", [])}


def parse_args(plugin, content):
    """The JSON args `content`, defaulted, typed like their defaults, and checked."""
    args = json.loads(content or "{}")
    if not isinstance(args, dict):
        raise ConfigError("the args must be a JSON object")
    for name, default in defaults(plugin).items():
        value = default if args.get(name) is None else args[name]
        args[name] = type(default)(value)
    for name, value in args.items():
        if isinstance(value, str) and name in ("server", "remote") and not value.strip():
            raise ConfigError(f"the {name!r} arg must be set")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
            raise ConfigError(f"{name} must not be negative")
    if args["max_output_size"] <= 0:
        raise ConfigError("max_output_size must be positive")
    if plugin == PREPROCESS_PLUGIN:
        if not 0 < args["patch_size"] <= args["image_resolution"]:
            raise ConfigError("patch_size must be positive and at most image_resolution")
        if args["image_resolution"] % args["patch_size"]:
            raise ConfigError("image_resolution must be a multiple of patch_size")
        if args["quality"] > 100:
            raise ConfigError("quality must be from 0 to 100")
    return args


def run_reachback(name, args, stop):
    remote_ip = os.environ.get("STEELEAGLE_REMOTE_QUALIFIER")
    if not remote_ip:
        sys.exit("reachback needs STEELEAGLE_REMOTE_QUALIFIER")
    # One database per service, replaced on each start
    path = Path(tempfile.gettempdir()) / f"swiftmap-reachback-{name}.sqlite3"
    path.unlink(missing_ok=True)
    store = reachback.FrameStore(str(path))
    keyframes = onboard.KeyframeFilter(min_disparity=args["min_disparity"],
                                       send_distance=args["send_distance"])
    try:
        reachback.run(name, reachback.Reachback(store, keyframes, max_size=args["max_output_size"]),
                      f"{remote_ip}.{args['remote']}", max_output_size=args["max_output_size"], stop=stop)
    finally:
        store.close()
        path.unlink(missing_ok=True)


def run_swiftmap(name, args, stop):
    client = remote.SwiftMapClient(args["server"], args["server_port"])
    if not client.connect(max_retries=1):
        logging.getLogger("swiftmap").warning(
            "SwiftMap server at %s:%s is not reachable yet; connecting on the first frame",
            args["server"], args["server_port"])
    with remote.Sender(client) as sender:
        remote.run(name, remote.SwiftMap(sender), max_output_size=args["max_output_size"], stop=stop)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    plugin = os.environ.get("STEELEAGLE_PLUGIN", SWIFTMAP_PLUGIN)
    if plugin not in PLUGINS:
        sys.exit(f"Unrecognized plugin {plugin!r}; expected one of {PLUGINS}")
    name = os.environ.get("STEELEAGLE_ENGINE_ENDPOINT")
    if not name:
        sys.exit("STEELEAGLE_ENGINE_ENDPOINT is not set")
    try:
        args = parse_args(plugin, os.environ.get("STEELEAGLE_PLUGIN_ARGS"))
    except (ConfigError, ValueError, TypeError) as err:
        sys.exit(f"Bad args for {plugin}: {err}")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if plugin == REACHBACK_PLUGIN:
        run_reachback(name, args, stop)
    elif plugin == PREPROCESS_PLUGIN:
        onboard.run_preprocess(name, args, stop=stop)
    else:
        run_swiftmap(name, args, stop)


if __name__ == "__main__":
    main()
