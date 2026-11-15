import numpy as np
import pytest
import supervision as sv

from detection.config import parse_args
from detection.predictors import (
    PAD_VALUE,
    Detected,
    LetterboxPredictor,
    from_supervision,
    keep_classes,
    load_predictor,
    pad_to_square,
    scale_to_fit,
)


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


class SquarePredictor:
    """A predictor with an input size that finds the same objects, with boxes
    as fractions of the image it is given, in every image."""

    def __init__(self, input_size, *detected):
        self.input_size = input_size
        self.detected = list(detected)
        self.images = []

    def predict(self, image):
        self.images.append(image)
        return list(self.detected)


def test_pad_to_square_centers_the_image_on_gray():
    rgb = np.full((20, 40, 3), 200, dtype=np.uint8)
    padded, top, left = pad_to_square(rgb, 64)
    assert padded.shape == (64, 64, 3) and (top, left) == (22, 12)
    assert (padded[top : top + 20, left : left + 40] == 200).all()
    assert padded[0, 0].tolist() == [PAD_VALUE] * 3
    assert (padded == 200).sum() == rgb.size


@pytest.mark.parametrize(
    ("shape", "scaled"),
    [
        ((20, 40), (32, 64)),
        ((40, 20), (64, 32)),
        ((30, 100), (19, 64)),
        ((64, 10), (64, 10)),
    ],
)
def test_scale_to_fit_scales_evenly_until_the_longer_side_fits(shape, scaled):
    rgb = np.full((*shape, 3), 200, dtype=np.uint8)
    out = scale_to_fit(rgb, 64)
    assert out.shape == (*scaled, 3) and out.dtype == np.uint8
    assert (out == 200).all()


def test_a_small_wide_image_is_scaled_up_evenly_then_padded():
    # 40x20 scales to 64x32, which sits at (top 16, left 0) of the 64x64 square
    whole = Detected("person", 0.9, (16 / 64, 0.0, 48 / 64, 1.0))
    spill = Detected("dog", 0.8, (0.0, 0.0, 48 / 64, 0.5))
    inner = SquarePredictor(64, whole, spill)
    rgb = np.full((20, 40, 3), 200, dtype=np.uint8)
    detected = LetterboxPredictor(inner).predict(rgb)
    image = inner.images[0]
    assert image.shape == (64, 64, 3)
    assert (image[16:48] == 200).all() and (image[:16] == PAD_VALUE).all()
    # Boxes are fractions of the image given, clipped out of the padding
    assert detected[0].box == pytest.approx((0, 0, 1, 1))
    assert detected[1].box == pytest.approx((0, 0, 1, 0.5))
    assert [d.class_name for d in detected] == ["person", "dog"]


def test_a_big_frame_is_scaled_down_evenly_to_the_input_size():
    inner = SquarePredictor(64)
    LetterboxPredictor(inner).predict(np.zeros((300, 1000, 3), dtype=np.uint8))
    assert inner.images[0].shape == (64, 64, 3)


def test_a_detection_wholly_in_the_padding_is_left_out():
    in_padding = Detected("person", 0.9, (0.0, 0.0, 0.2, 0.2))
    predictor = LetterboxPredictor(SquarePredictor(64, in_padding))
    assert predictor.predict(np.zeros((20, 40, 3), dtype=np.uint8)) == []


def test_load_predictor_letterboxes_only_with_letterbox(monkeypatch):
    from detection import predictors

    monkeypatch.setattr(
        predictors, "_load", lambda config, models_dir: SquarePredictor(64)
    )
    assert isinstance(load_predictor(parse_args("{}"), None), SquarePredictor)
    boxed = load_predictor(parse_args('{"letterbox": true}'), None)
    assert isinstance(boxed, LetterboxPredictor) and boxed.input_size == 64
