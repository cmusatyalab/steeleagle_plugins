"""Entrypoint of the storage plugins, which eagled starts with `pixi run start`.

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
import tempfile
import threading
from pathlib import Path

from storage.engine import qualify, run
from storage.priority_reintegration import PriorityReintegration
from storage.reachback import Reachback
from storage.store import FrameStore

PRIORITY_REINTEGRATION_PLUGIN = "priority_reintegration.plugin.toml"
REACHBACK_PLUGIN = "reachback.plugin.toml"

# The largest H.264 frame the h264 encoder sends: a 1080p RGB frame, and room
# for its other fields.
DEFAULT_MAX_OUTPUT_SIZE = 1920 * 1080 * 3 + (16 << 10)

# Defaults of the args of each plugin, matching its plugin.toml. The daemon
# passes only the args set in its config, so any arg left unset is defaulted
# here.
DEFAULTS = {
    PRIORITY_REINTEGRATION_PLUGIN: {
        "remote": "",
        "classifier": "",
        "class": "",
        "database": "",
        "max_output_size": DEFAULT_MAX_OUTPUT_SIZE,
    },
    REACHBACK_PLUGIN: {
        "remote": "",
        "database": "",
        "max_output_size": DEFAULT_MAX_OUTPUT_SIZE,
    },
}
REQUIRED = {
    PRIORITY_REINTEGRATION_PLUGIN: ("remote", "classifier", "class"),
    REACHBACK_PLUGIN: ("remote",),
}


class ConfigError(Exception):
    pass


def parse_args(plugin, content):
    """The args in the JSON object `content`, with every unset arg of
    `plugin` defaulted."""
    args = json.loads(content or "{}")
    for name, default in DEFAULTS[plugin].items():
        if args.get(name) is None:
            args[name] = default
    for name in REQUIRED[plugin]:
        if not str(args[name]).strip():
            raise ConfigError(f"the {name!r} arg must be set")
    args["max_output_size"] = int(args["max_output_size"])
    if args["max_output_size"] <= 0:
        raise ConfigError("max_output_size must be positive")
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
    except (ConfigError, ValueError) as err:
        sys.exit(f"Bad args for {plugin}: {err}")
    local = os.environ.get("STEELEAGLE_LOCAL_QUALIFIER", "")
    remote = qualify(os.environ.get("STEELEAGLE_REMOTE_QUALIFIER", ""), args["remote"])

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    # With no database given, frames go to a temporary one, removed on exit
    with tempfile.TemporaryDirectory(prefix="steeleagle-storage-") as scratch:
        path = args["database"] or str(Path(scratch) / "frames.sqlite3")
        logging.getLogger("storage").info("storing frames in %s", path)
        with FrameStore(path) as store:
            if plugin == PRIORITY_REINTEGRATION_PLUGIN:
                classifier = qualify(local, args["classifier"])
                handler = PriorityReintegration(
                    store,
                    remote=remote,
                    classifier=classifier,
                    target_class=args["class"],
                    max_frame_size=args["max_output_size"],
                )
                subscriptions = [remote, classifier]
            else:
                handler = Reachback(
                    store, remote=remote, max_frame_size=args["max_output_size"]
                )
                subscriptions = [remote]
            run(
                name,
                handler,
                subscriptions,
                max_output_size=args["max_output_size"],
                stop=stop,
            )


if __name__ == "__main__":
    main()
