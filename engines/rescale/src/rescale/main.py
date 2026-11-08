"""Entrypoint of the rescale engine, which eagled starts with `pixi run start`.

eagled passes the plugin's args in STEELEAGLE_PLUGIN_ARGS (a JSON object) and
the name to serve the engine as in STEELEAGLE_ENGINE_ENDPOINT.
"""

import json
import logging
import os
import signal
import sys
import threading


from rescale.engine import run
from rescale.rescale import DEFAULT_ASPECT_RATIO, RESOLUTIONS, dimensions


class ConfigError(Exception):
    pass


def parse_args(content):
    """The args in the JSON object `content`, with the resolution, which must
    be set, at the aspect ratio, 16:9 if unset, as dimensions (width,
    height)."""
    args = json.loads(content or "{}")
    if not isinstance(args, dict):
        raise ConfigError("the args must be a JSON object")
    resolution = args.get("resolution")
    if resolution is None or (isinstance(resolution, str) and not resolution.strip()):
        raise ConfigError(f"the 'resolution' arg must be set to one of {', '.join(RESOLUTIONS)}")
    aspect_ratio = args.get("aspect_ratio")
    if aspect_ratio is None or (isinstance(aspect_ratio, str) and not aspect_ratio.strip()):
        aspect_ratio = args["aspect_ratio"] = DEFAULT_ASPECT_RATIO
    try:
        args["dimensions"] = dimensions(resolution, aspect_ratio)
    except ValueError as err:
        raise ConfigError(str(err)) from None
    return args


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    name = os.environ.get("STEELEAGLE_ENGINE_ENDPOINT")
    if not name:
        sys.exit("STEELEAGLE_ENGINE_ENDPOINT is not set")
    try:
        args = parse_args(os.environ.get("STEELEAGLE_PLUGIN_ARGS"))
    except (ConfigError, ValueError) as err:
        sys.exit(f"Bad args for the rescale engine: {err}")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run(name, args["dimensions"], stop=stop)


if __name__ == "__main__":
    main()
