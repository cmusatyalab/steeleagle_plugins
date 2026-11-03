import pytest

from fusion.main import FUSION_DETECTION_PLUGIN, ConfigError, parse_args


def test_defaults():
    args = parse_args(FUSION_DETECTION_PLUGIN, '{"local": "det", "remote": "det"}')
    assert args == {"local": "det", "remote": "det", "grace": 200, "timeout": 1000}


def test_grace_is_whole_milliseconds():
    args = '{{"local": "a", "remote": "b", "grace": {}}}'
    assert parse_args(FUSION_DETECTION_PLUGIN, args.format("50.0"))["grace"] == 50
    for bad in ("1.5", "-1", "true"):
        with pytest.raises(ConfigError):
            parse_args(FUSION_DETECTION_PLUGIN, args.format(bad))


def test_timeout_is_positive_whole_milliseconds():
    args = '{{"local": "a", "remote": "b", "timeout": {}}}'
    assert parse_args(FUSION_DETECTION_PLUGIN, args.format("500.0"))["timeout"] == 500
    for bad in ("1.5", "-1", "0", "true"):
        with pytest.raises(ConfigError):
            parse_args(FUSION_DETECTION_PLUGIN, args.format(bad))


@pytest.mark.parametrize("content", ["", '{"local": "a"}', '{"remote": "b", "local": " "}'])
def test_engines_are_required(content):
    with pytest.raises(ConfigError):
        parse_args(FUSION_DETECTION_PLUGIN, content)
