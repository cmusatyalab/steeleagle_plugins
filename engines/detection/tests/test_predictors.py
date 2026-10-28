import numpy as np
import pytest
import supervision as sv

from detection.predictors import Detected, from_supervision, keep_classes


def test_keep_classes_with_no_filter_keeps_everything():
    detected = [
        Detected("person", 0.9, (0, 0, 1, 1)),
        Detected("car", 0.8, (0, 0, 1, 1)),
    ]
    assert keep_classes(detected, []) == detected


def test_keep_classes_keeps_only_the_named_classes():
    person = Detected("person", 0.9, (0, 0, 1, 1))
    car = Detected("car", 0.8, (0, 0, 1, 1))
    assert keep_classes([person, car], ["car"]) == [car]


def test_keep_classes_ignores_case_and_surrounding_space():
    person = Detected("Person", 0.9, (0, 0, 1, 1))
    assert keep_classes([person], [" person "]) == [person]


def test_from_supervision_normalizes_boxes_to_the_image_size():
    detections = sv.Detections(
        xyxy=np.array([[10.0, 6.0, 30.0, 24.0]]),
        confidence=np.array([0.75]),
        class_id=np.array([0]),
        data={"class_name": np.array(["dog"], dtype=object)},
    )
    (detected,) = from_supervision(detections, width=40, height=30)
    assert detected.class_name == "dog"
    assert detected.score == pytest.approx(0.75)
    # y_min, x_min, y_max, x_max as fractions of the image
    assert detected.box == pytest.approx((6 / 30, 10 / 40, 24 / 30, 30 / 40))


def test_from_supervision_of_no_detections_is_empty():
    assert from_supervision(sv.Detections.empty(), width=40, height=30) == []
