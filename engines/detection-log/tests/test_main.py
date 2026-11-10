import json
import tomllib
from pathlib import Path

import pytest

from detection_log.main import (
    DEFAULTS,
    PLUGIN_FILE,
    ConfigError,
    clear_directory,
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
    assert args == {"detector": "detection", "timeout": 2000, "refresh": False}


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"detector": "  "},
        {"detector": "d", "timeout": 0},
        {"detector": "d", "timeout": 1.5},
        {"detector": "d", "timeout": True},
        {"detector": "d", "refresh": "yes"},
    ],
)
def test_bad_args_are_refused(args):
    with pytest.raises(ConfigError):
        parse_args(json.dumps(args))


def test_the_directory_is_the_last_part_of_the_engine_endpoint():
    assert engine_dir_name("100.64.0.1.detection_log") == "detection_log"
    assert engine_dir_name("detection_log") == "detection_log"


@pytest.mark.parametrize("endpoint", ["", "100.64.0.1.", "a/..", "a/b"])
def test_an_endpoint_that_names_no_directory_is_refused(endpoint):
    with pytest.raises(ConfigError):
        engine_dir_name(endpoint)


def test_refreshing_empties_the_directory_but_keeps_it(tmp_path):
    (tmp_path / "1.jpg").write_bytes(b"x")
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "2.jpg").write_bytes(b"x")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"x")
    (tmp_path / "link").symlink_to(outside)
    clear_directory(tmp_path)
    assert tmp_path.is_dir() and list(tmp_path.iterdir()) == []
    # A link is removed, not followed
    assert (outside / "keep").is_file()


def test_refreshing_a_missing_directory_does_nothing(tmp_path):
    clear_directory(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


def test_the_detector_is_qualified_with_the_local_daemon():
    assert qualify("100.64.0.1", "detection") == "100.64.0.1.detection"
    assert qualify("", "detection") == "detection"


def test_the_files_directory_is_steeleagle_files(tmp_path):
    assert files_dir({"STEELEAGLE_FILES": str(tmp_path)}) == tmp_path
