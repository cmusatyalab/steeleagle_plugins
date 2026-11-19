import pytest
from conftest import FRAME_HEIGHT, FRAME_WIDTH, MARKER_SIDE, make_frame

from aruco.engine import FrameError, detect, make_detector


@pytest.fixture
def detector():
    return make_detector("DICT_ARUCO_ORIGINAL")


def test_a_frame_without_markers_has_no_detections(detector):
    assert len(detect(make_frame(), detector).detections) == 0


def test_a_marker_is_a_detection_of_its_id_with_its_box(detector):
    (detection,) = detect(make_frame([(7, 100, 50)]), detector).detections
    assert detection.class_name == "aruco_7"
    assert detection.score == 1.0
    tolerance = 2 / FRAME_WIDTH
    assert detection.bbox.x_min == pytest.approx(100 / FRAME_WIDTH, abs=tolerance)
    assert detection.bbox.x_max == pytest.approx(
        (100 + MARKER_SIDE) / FRAME_WIDTH, abs=tolerance
    )
    assert detection.bbox.y_min == pytest.approx(50 / FRAME_HEIGHT, abs=tolerance)
    assert detection.bbox.y_max == pytest.approx(
        (50 + MARKER_SIDE) / FRAME_HEIGHT, abs=tolerance
    )


def test_every_marker_is_found(detector):
    frame = make_frame([(0, 20, 20), (3, 200, 120)])
    names = sorted(d.class_name for d in detect(frame, detector).detections)
    assert names == ["aruco_0", "aruco_3"]


def test_only_the_requested_ids_are_reported(detector):
    frame = make_frame([(0, 20, 20), (3, 200, 120)])
    (detection,) = detect(frame, detector, only_ids=[3]).detections
    assert detection.class_name == "aruco_3"


def test_markers_of_another_dictionary_are_not_found(detector):
    frame = make_frame([(5, 100, 50)], dictionary="DICT_6X6_250")
    assert len(detect(frame, detector).detections) == 0
    (detection,) = detect(frame, make_detector("DICT_6X6_250")).detections
    assert detection.class_name == "aruco_5"


def test_a_frame_whose_size_does_not_match_its_data_is_an_error(detector):
    frame = make_frame()
    frame.h_res += 1
    with pytest.raises(FrameError):
        detect(frame, detector)
