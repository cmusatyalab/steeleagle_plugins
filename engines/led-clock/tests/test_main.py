import json
import tomllib
from pathlib import Path

import pytest

from led_clock.main import DEFAULTS, PLUGIN_FILE, ConfigError, engine_dir_name, parse_args

PLUGIN_DIR = Path(__file__).parent.parent


def test_the_manifest_defaults_are_the_defaults():
    manifest = tomllib.loads((PLUGIN_DIR / PLUGIN_FILE).read_text())
    assert {arg["name"]: arg["default"] for arg in manifest["args"]} == DEFAULTS


def test_unset_args_are_defaulted():
    assert parse_args(json.dumps({"fps": 15})) == {**DEFAULTS, "fps": 15}
    assert parse_args(None) == DEFAULTS


@pytest.mark.parametrize("name", list(DEFAULTS))
@pytest.mark.parametrize("value", [0, -1, True, "5"])
def test_args_must_be_positive_numbers(name, value):
    with pytest.raises(ConfigError):
        parse_args(json.dumps({name: value}))


def test_args_must_be_an_object():
    with pytest.raises(ConfigError):
        parse_args("[]")


@pytest.mark.parametrize("endpoint", ["", ".", "..", "a/b"])
def test_an_endpoint_that_names_no_directory_is_refused(endpoint):
    with pytest.raises(ConfigError):
        engine_dir_name(endpoint)
