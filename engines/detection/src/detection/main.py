"""Entrypoint of the detection plugin, which eagled starts with `pixi run start`.

eagled names the plugin to run in STEELEAGLE_PLUGIN, its args in
STEELEAGLE_PLUGIN_ARGS (a JSON object), and the name to serve the engine as in
STEELEAGLE_ENGINE_ENDPOINT.
"""

import logging
import os
import signal
import sys
import threading

from detection import paths
from detection.config import parse_args
from detection.engine import run
from detection.geofence import Geofence
from detection.geolocate import Camera
from detection.predictors import load_predictor

PLUGIN_FILE = "detection.plugin.toml"


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
    geofence = None
    if config.geofence:
        geofence = Geofence.from_kml(paths.resolve(paths.files_dir(), config.geofence))
    predictor = load_predictor(config, paths.models_dir())

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run(
        name,
        predictor,
        config.only_classes,
        geofence,
        Camera(hfov_deg=config.hfov, vfov_deg=config.vfov),
        stop=stop,
        max_output_size=config.max_output_size,
    )


if __name__ == "__main__":
    main()
