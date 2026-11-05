"""Entrypoint of the MCAP recorder, which eagled starts with `pixi run start`.

eagled passes the plugin's args in STEELEAGLE_PLUGIN_ARGS (a JSON object),
the name to serve the engine as in STEELEAGLE_ENGINE_ENDPOINT, the IP that
qualifies the names of local engines in STEELEAGLE_LOCAL_QUALIFIER, and its
files directory in STEELEAGLE_FILES, whose parent is the orchestrator's data
directory.
"""

import json
import logging
import os
import signal
import sys
import threading
from collections.abc import Mapping
from pathlib import Path

from recorder.engine import run
from recorder.files import VehicleFiles
from recorder.recorder import Recorder

DEFAULTS = {
    "directory": "",
    "engines": ["yolo"],
    "image_width": 320,
    "idle_timeout": 60,
}


class ConfigError(Exception):
    pass


def qualify(qualifier: str, name: str) -> str:
    """`name` qualified with a daemon's IP, as eagled names its services, or
    as is if there is no qualifier."""
    return f"{qualifier}.{name}" if qualifier else name


def default_directory(env: Mapping[str, str]) -> Path:
    """Where recordings go by default: logs/mcap in the orchestrator's data
    directory."""
    if env.get("STEELEAGLE_FILES"):
        data = Path(env["STEELEAGLE_FILES"]).parent
    elif env.get("XDG_DATA_HOME"):
        data = Path(env["XDG_DATA_HOME"]) / "steeleagle"
    else:
        data = Path("~/.local/share/steeleagle").expanduser()
    return data / "logs" / "mcap"


def parse_args(content: str | None, env: Mapping[str, str] = os.environ) -> dict:
    """The args in the JSON object `content`, with every unset arg defaulted."""
    args = json.loads(content or "{}")
    for name, default in DEFAULTS.items():
        if args.get(name) is None:
            args[name] = default
    if not args["directory"]:
        args["directory"] = str(default_directory(env))
    engines = args["engines"]
    if not isinstance(engines, list) or not all(
        isinstance(e, str) and e.strip() for e in engines
    ):
        raise ConfigError("engines must be a list of engine names")
    args["image_width"] = int(args["image_width"])
    if args["image_width"] < 0:
        raise ConfigError("image_width must be 0 (full size) or positive")
    args["idle_timeout"] = float(args["idle_timeout"])
    if args["idle_timeout"] <= 0:
        raise ConfigError("idle_timeout must be positive")
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
        sys.exit(f"Bad args for the MCAP recorder: {err}")
    local = os.environ.get("STEELEAGLE_LOCAL_QUALIFIER", "")
    engines = {qualify(local, e): e for e in args["engines"]}

    # eagled stops plugins with SIGKILL; these only matter for manual runs.
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    directory = Path(args["directory"])
    logging.getLogger("recorder").info("recording to %s", directory)
    files = VehicleFiles(directory, args["idle_timeout"])
    recorder = Recorder(files, engines, image_width=args["image_width"])
    run(name, recorder, files, list(engines), stop=stop)


if __name__ == "__main__":
    main()
