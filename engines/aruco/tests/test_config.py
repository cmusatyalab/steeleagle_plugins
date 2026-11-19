import json

import pytest

from aruco.config import (
    DEFAULT_DICTIONARY,
    Config,
    ConfigError,
    dictionaries,
    parse_args,
)


def test_no_args_take_the_defaults():
    assert parse_args(None) == Config()
    assert parse_args("{}").dictionary == DEFAULT_DICTIONARY


def test_null_args_take_the_defaults():
    assert parse_args(json.dumps({"only_ids": None})) == Config()


def test_args_are_read():
    config = parse_args(
        json.dumps(
            {
                "dictionary": "DICT_4X4_50",
                "only_ids": [0, 7.0],
                "max_output_size": 1024.0,
                "early_discard": True,
                "unknown": "ignored",
            }
        )
    )
    assert config == Config("DICT_4X4_50", [0, 7], 1024, True)
    assert all(isinstance(i, int) for i in config.only_ids)
    assert isinstance(config.max_output_size, int)


def test_the_opencv_dictionaries_are_listed():
    assert {"DICT_ARUCO_ORIGINAL", "DICT_6X6_250", "DICT_APRILTAG_36h11"} <= set(
        dictionaries()
    )


@pytest.mark.parametrize(
    "args",
    [
        {"dictionary": "DICT_NOPE"},
        {"only_ids": [-1]},
        {"only_ids": [1.5]},
        {"only_ids": [True]},
        {"only_ids": "0"},
        {"max_output_size": 0},
        {"max_output_size": 10.5},
        {"early_discard": "yes"},
    ],
)
def test_bad_args_are_rejected(args):
    with pytest.raises(ConfigError):
        parse_args(json.dumps(args))


def test_args_that_are_not_an_object_are_rejected():
    with pytest.raises(ConfigError):
        parse_args("[]")
