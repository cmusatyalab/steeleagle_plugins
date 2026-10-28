import json
import tomllib
from pathlib import Path

import pytest

from detection.config import Config, ConfigError, parse_args

MANIFEST = Path(__file__).parent.parent / "detection.plugin.toml"


def test_unset_args_take_their_defaults():
    config = parse_args(None)
    assert config == parse_args("{}")
    assert config.confidence == pytest.approx(0.5)
    assert config.only_classes == []
    assert config.geofence == ""
    assert config.model.endswith(".pt")


def test_an_arg_the_daemon_passes_overrides_its_default():
    config = parse_args(
        json.dumps({"confidence": 0.9, "only_classes": ["person"], "model": "mine.pt"})
    )
    assert (config.confidence, config.only_classes, config.model) == (
        0.9,
        ["person"],
        "mine.pt",
    )


def test_a_null_arg_takes_its_default():
    assert (
        parse_args(json.dumps({"confidence": None})).confidence
        == parse_args(None).confidence
    )


def test_pt_models_are_yolo_and_pth_models_are_rfdetr():
    assert parse_args(json.dumps({"model": "a.pt"})).architecture == "yolo"
    assert parse_args(json.dumps({"model": "a.pth"})).architecture == "rfdetr"


def test_an_unrecognized_extension_is_an_error():
    with pytest.raises(ConfigError, match="model"):
        parse_args(json.dumps({"model": "a.onnx"}))


def test_an_unknown_rfdetr_size_is_an_error():
    with pytest.raises(ConfigError, match="rfdetr_size"):
        parse_args(json.dumps({"model": "a.pth", "rfdetr_size": "huge"}))


@pytest.mark.parametrize("confidence", [0, -0.1, 1.5])
def test_confidence_outside_zero_to_one_is_an_error(confidence):
    with pytest.raises(ConfigError, match="confidence"):
        parse_args(json.dumps({"confidence": confidence}))


@pytest.mark.parametrize("field", ["hfov", "vfov"])
@pytest.mark.parametrize("degrees", [0, 180, -5])
def test_field_of_view_outside_open_zero_to_180_is_an_error(field, degrees):
    with pytest.raises(ConfigError, match=field):
        parse_args(json.dumps({field: degrees}))


def test_config_is_a_dataclass_with_every_arg():
    assert {
        "model",
        "rfdetr_size",
        "rfdetr_labels",
        "only_classes",
        "confidence",
        "geofence",
        "hfov",
        "vfov",
    } <= set(Config.__dataclass_fields__)


def test_the_manifest_defaults_are_the_config_defaults():
    with open(MANIFEST, "rb") as f:
        manifest = tomllib.load(f)
    defaults = {arg["name"]: arg["default"] for arg in manifest["args"]}
    assert defaults == {name: getattr(Config(), name) for name in defaults}


def test_the_default_model_is_accepted():
    parse_args(json.dumps({"model": Config().model}))
