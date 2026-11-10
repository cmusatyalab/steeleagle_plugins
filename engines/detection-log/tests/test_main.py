import json
import tomllib
from pathlib import Path

import pytest

from detection_log.main import (
    DEFAULTS,
    PLUGIN_FILE,
    ConfigError,
    engine_dir_name,
    files_dir,
    parse_args,
    qualify,
)

PLUGIN_DIR = Path(__file__).parent.parent


def test_the_manifest_defaults_are_the_defaults():
    manifest = tomllib.loads((PLUGIN_DIR / PLUGIN_FILE).read_text())
    assert {arg["name"]: arg["default"] for arg in manifest["args"]} == DEFAULTS


def test_unset_args_are_defaulted():
    args = parse_args(json.dumps({"detector": "detection"}))
    assert args == {"detector": "detection", "timeout": 2000}


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"detector": "  "},
        {"detector": "d", "timeout": 0},
        {"detector": "d", "timeout": 1.5},
        {"detector": "d", "timeout": True},
    ],
)
def test_bad_args_are_refused(args):
    with pytest.raises(ConfigError):
        parse_args(json.dumps(args))


def test_the_directory_is_the_last_part_of_the_engine_endpoint():
    assert engine_dir_name("100.64.0.1.detection_log") == "detection_log"
    assert engine_dir_name("detection_log") == "detection_log"


def test_the_detector_is_qualified_with_the_local_daemon():
    assert qualify("100.64.0.1", "detection") == "100.64.0.1.detection"
    assert qualify("", "detection") == "detection"


def test_the_files_directory_is_steeleagle_files(tmp_path):
    assert files_dir({"STEELEAGLE_FILES": str(tmp_path)}) == tmp_path
