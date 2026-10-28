"""Runs real YOLO26 weights on the bus photo that ships with Ultralytics.
Stock weights are downloaded the first time; the test skips without network."""

import cv2
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
