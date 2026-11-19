"""Entrypoint of the ArUco plugin, which eagled starts with `pixi run start`.

eagled names the plugin to run in STEELEAGLE_PLUGIN, its args in
STEELEAGLE_PLUGIN_ARGS (a JSON object), and the name to serve the engine as in
STEELEAGLE_ENGINE_ENDPOINT.
"""

import logging
import os
import signal
import sys
import threading

from aruco.config import parse_args
from aruco.engine import make_detector, run

PLUGIN_FILE = "aruco.plugin.toml"


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

    config = parse_args(os.environ.get("STEELEAGLE_PLUGIN_ARGS"))
    logging.getLogger("aruco").info(
        "finding %s markers%s",
        config.dictionary,
        f" with ids {config.only_ids}" if config.only_ids else "",
    )

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run(
        name,
        make_detector(config.dictionary),
        config.only_ids,
        stop=stop,
        max_output_size=config.max_output_size,
        early_discard=config.early_discard,
    )


if __name__ == "__main__":
    main()
