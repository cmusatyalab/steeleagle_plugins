"""The plugin's args, as the daemon passes them in STEELEAGLE_PLUGIN_ARGS."""

import json
from dataclasses import dataclass, field, fields

import cv2

# The dictionary the old Gabriel engine used, so existing markers still work.
DEFAULT_DICTIONARY = "DICT_ARUCO_ORIGINAL"
DEFAULT_MAX_OUTPUT_BYTES = 1 << 16
DICTIONARY_PREFIX = "DICT_"


class ConfigError(Exception):
    pass


@dataclass
class Config:
    dictionary: str = DEFAULT_DICTIONARY
    only_ids: list[int] = field(default_factory=list)
    max_output_size: int = DEFAULT_MAX_OUTPUT_BYTES
    early_discard: bool = False


def dictionaries():
    """The names of OpenCV's predefined marker dictionaries."""
    return sorted(
        name
        for name in dir(cv2.aruco)
        if name.startswith(DICTIONARY_PREFIX)
        and isinstance(getattr(cv2.aruco, name), int)
    )


def parse_args(content):
    """The Config for the JSON object `content`; any arg it leaves unset (or
    null) takes its default."""
    given = json.loads(content or "{}")
    if not isinstance(given, dict):
        raise ConfigError(f"args {content!r} are not a JSON object")
    known = {f.name for f in fields(Config)}
    config = Config(**{k: v for k, v in given.items() if k in known and v is not None})
    _validate(config)
    return config


def _is_whole(value):
    # bool is an int, but true is not a marker id or a size
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and value.is_integer())


def _validate(config):
    if config.dictionary not in dictionaries():
        raise ConfigError(
            f"dictionary {config.dictionary!r} is not one of OpenCV's: "
            + ", ".join(dictionaries())
        )
    if not isinstance(config.only_ids, list) or not all(
        _is_whole(i) and i >= 0 for i in config.only_ids
    ):
        raise ConfigError(f"only_ids {config.only_ids!r} must be a list of marker ids")
    # JSON numbers may arrive as floats, e.g. 7.0 or 65536.0
    config.only_ids = [int(i) for i in config.only_ids]
    if not _is_whole(config.max_output_size) or config.max_output_size <= 0:
        raise ConfigError(
            f"max_output_size {config.max_output_size!r} must be a positive number of bytes"
        )
    config.max_output_size = int(config.max_output_size)
    if not isinstance(config.early_discard, bool):
        raise ConfigError(
            f"early_discard {config.early_discard!r} must be true or false"
        )
