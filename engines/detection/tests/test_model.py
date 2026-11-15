"""Runs real YOLO26 weights on the bus photo that ships with Ultralytics.
Stock weights are downloaded the first time; the test skips without network."""

import cv2
import numpy as np
import pytest
from conftest import CAMERA

from detection.config import parse_args
from detection.engine import detect
from detection.predictors import load_predictor
from steeleagle_protocol.v1.messages.frame import frame_pb2

pytestmark = pytest.mark.model

RGB_CHANNELS = 3
BUS_PHOTO_MIN_PEOPLE = 3


def bus_frame():
    from ultralytics.utils import ASSETS

    bgr = cv2.imread(str(ASSETS / "bus.jpg"))
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    height, width, _ = rgb.shape
    return frame_pb2.RawFrame(
        data=rgb.tobytes(), h_res=width, v_res=height, channels=RGB_CHANNELS
    )


@pytest.fixture(scope="module")
def yolo(tmp_path_factory):
    models = tmp_path_factory.mktemp("models")
    try:
        return load_predictor(
            parse_args('{"model": "yolo26n.pt", "confidence": 0.4}'), models
        )
    except Exception as err:  # no network for the stock download
        pytest.skip(f"cannot load yolo26n.pt: {err}")


def test_yolo26_finds_the_bus_and_the_people_in_the_bus_photo(yolo):
    result = detect(bus_frame(), yolo, [], None, CAMERA)
    names = [d.class_name for d in result.detections]
    assert "bus" in names
    assert names.count("person") >= BUS_PHOTO_MIN_PEOPLE


def test_the_class_filter_leaves_only_the_bus(yolo):
    result = detect(bus_frame(), yolo, ["bus"], None, CAMERA)
    assert {d.class_name for d in result.detections} == {"bus"}


@pytest.fixture(scope="module")
def rfdetr(tmp_path_factory):
    models = tmp_path_factory.mktemp("rfdetr-models")
    try:
        return load_predictor(
            parse_args('{"model": "rfdetr", "rfdetr_size": "nano", "confidence": 0.4}'),
            models,
        )
    except Exception as err:  # no network for the stock download
        pytest.skip(f"cannot load stock RF-DETR nano: {err}")


def test_rfdetr_finds_the_bus_and_the_people_in_the_bus_photo(rfdetr):
    result = detect(bus_frame(), rfdetr, [], None, CAMERA)
    names = [d.class_name for d in result.detections]
    assert "bus" in names
    assert names.count("person") >= BUS_PHOTO_MIN_PEOPLE


def test_rfdetr_finds_a_small_letterboxed_crop_of_a_person(rfdetr):
    from detection.predictors import LetterboxPredictor

    boxed = LetterboxPredictor(rfdetr)
    # Nano runs at 384x384
    assert boxed.input_size == rfdetr.model.model_config.resolution == 384
    # Writable, as the engine's frames are
    rgb = np.frombuffer(bus_frame().data, dtype=np.uint8).reshape(1080, 810, 3).copy()
    people = [
        d for d in rfdetr.predict(rgb) if d.class_name == "person" and d.score > 0.5
    ]
    assert people
    y_min, x_min, y_max, x_max = people[0].box
    crop = rgb[
        int(y_min * 1080) : int(y_max * 1080), int(x_min * 810) : int(x_max * 810)
    ]
    # Shrunk to 64 px tall, as a distant person's crop would be
    small = cv2.resize(
        crop, (max(1, crop.shape[1] * 64 // crop.shape[0]), 64), cv2.INTER_AREA
    )
    detected = boxed.predict(small)
    assert max(d.score for d in detected if d.class_name == "person") > 0.5
    assert all(0 <= v <= 1 for d in detected for v in d.box)


def test_yolo_reports_its_input_size(yolo):
    assert yolo.input_size == 640


def test_a_trained_rfdetr_2xlarge_checkpoint_loads_as_its_own_variant(tmp_path):
    """A .pth records the variant it was trained as. 2XLarge uses patch size
    20, which no rfdetr_size class could load before."""
    import rfdetr
    import torch

    trained = rfdetr.RFDETR2XLarge(pretrain_weights=None, num_classes=2, device="cpu")
    torch.save(
        {
            "model": trained.model.model.state_dict(),
            "args": {"num_classes": 2},
            "model_name": "RFDETR2XLarge",
            "model_config": trained.model_config.model_dump(mode="json"),
        },
        tmp_path / "trained.pth",
    )

    predictor = load_predictor(parse_args('{"model": "trained.pth"}'), tmp_path)

    assert type(predictor.model).__name__ == "RFDETR2XLarge"
    assert predictor.model.model_config.patch_size == 20
