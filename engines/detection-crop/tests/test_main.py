import json

import pytest

from detection_crop.main import ConfigError, parse_args, qualify


def args(**kwargs):
    return json.dumps(
        {"detector": "detection", "class": "person", "verifier": "verifier", **kwargs}
    )


def test_defaults():
    assert parse_args(args()) == {
        "detector": "detection",
        "class": "person",
        "verifier": "verifier",
        "timeout": 2000,
        "max_width": 1920,
        "max_height": 1080,
        "max_queue": 64,
        "log_sent": "",
    }


@pytest.mark.parametrize("missing", ["detector", "class", "verifier"])
def test_detector_class_and_verifier_are_required(missing):
    with pytest.raises(ConfigError):
        parse_args(args(**{missing: " "}))


@pytest.mark.parametrize(
    "name", ["timeout", "max_width", "max_height", "max_queue"]
)
@pytest.mark.parametrize("value", [0, -1, 1.5, True])
def test_numbers_must_be_positive_whole_numbers(name, value):
    with pytest.raises(ConfigError):
        parse_args(args(**{name: value}))


def test_qualify():
    assert qualify("100.64.0.1", "detection") == "100.64.0.1.detection"
    assert qualify("", "detection") == "detection"
