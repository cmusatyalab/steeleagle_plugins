import pytest
from conftest import CAMERA, FixedPredictor, make_detected, make_frame

from detection.engine import FrameError, detect
from detection.geofence import Geofence


def run(frame, predictor, classes=(), geofence=None):
    return detect(frame, predictor, list(classes), geofence, CAMERA)


def test_each_detection_becomes_a_result_detection_with_its_box():
    predictor = FixedPredictor(make_detected("person", 0.9, (0.1, 0.2, 0.6, 0.7)))
    result = run(make_frame(), predictor)
    (detection,) = result.detections
    assert detection.class_name == "person"
    assert detection.score == pytest.approx(0.9)
    box = detection.bbox
    assert (box.y_min, box.x_min, box.y_max, box.x_max) == pytest.approx(
        (0.1, 0.2, 0.6, 0.7)
    )


def test_the_predictor_sees_the_frames_rgb_pixels_with_its_shape():
    predictor = FixedPredictor()
    run(make_frame(width=64, height=48), predictor)
    (image,) = predictor.images
    assert image.shape == (48, 64, 3)


def test_class_filter_drops_other_classes():
    predictor = FixedPredictor(make_detected("person"), make_detected("car"))
    result = run(make_frame(), predictor, classes=["car"])
    assert [d.class_name for d in result.detections] == ["car"]


def test_a_frame_that_is_not_rgb_is_an_error():
    frame = make_frame()
    frame.channels = 4
    with pytest.raises(FrameError):
        run(frame, FixedPredictor())


def test_a_frame_whose_data_does_not_match_its_size_is_an_error():
    frame = make_frame()
    frame.h_res += 1
    with pytest.raises(FrameError):
        run(frame, FixedPredictor())


def test_geofence_keeps_a_target_inside_and_drops_one_outside(square_kml):
    fence = Geofence.from_kml(square_kml)
    # Gimbal straight down, box centered: the target is under the vehicle,
    # which starts inside the fence.
    inside = run(
        make_frame(pitch=-90.0), FixedPredictor(make_detected()), geofence=fence
    )
    assert len(inside.detections) == 1
    # Gimbal 20 degrees down from 30 m up: the target is about 80 m ahead,
    # far outside a fence that is about 10 m across.
    outside = run(
        make_frame(pitch=-20.0), FixedPredictor(make_detected()), geofence=fence
    )
    assert len(outside.detections) == 0


def test_geofence_uses_the_vehicle_position_when_the_frame_has_no_gimbal(square_kml):
    fence = Geofence.from_kml(square_kml)
    result = run(
        make_frame(with_gimbal=False), FixedPredictor(make_detected()), geofence=fence
    )
    assert len(result.detections) == 1


def test_target_with_no_ground_intersection_is_dropped_when_a_geofence_is_set(
    square_kml,
):
    fence = Geofence.from_kml(square_kml)
    above_horizon = make_detected(box=(0.0, 0.4, 0.1, 0.6))
    result = run(make_frame(pitch=0.0), FixedPredictor(above_horizon), geofence=fence)
    assert len(result.detections) == 0


def test_without_a_geofence_a_target_with_no_ground_intersection_is_kept():
    above_horizon = make_detected(box=(0.0, 0.4, 0.1, 0.6))
    result = run(make_frame(pitch=0.0), FixedPredictor(above_horizon))
    assert len(result.detections) == 1


def test_the_predictor_gets_a_writable_image():
    # PyTorch warns about, and may corrupt, tensors made from read-only arrays
    predictor = FixedPredictor()
    run(make_frame(), predictor)
    assert predictor.images[0].flags.writeable
