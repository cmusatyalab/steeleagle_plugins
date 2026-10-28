import numpy as np
import pytest

from detection.geolocate import Camera
from detection.predictors import Detected
from steeleagle_protocol.v1.common import common_pb2
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.telemetry import telemetry_pb2

RGB_CHANNELS = 3
FRAME_WIDTH = 64
FRAME_HEIGHT = 48
START_LAT = 40.5
START_LON = -79.5
HEIGHT_M = 30.0
CAMERA = Camera(hfov_deg=69.0, vfov_deg=43.0)

# KML orders points lon,lat,alt. The square spans lat 40.4999..40.5001 and
# lon -79.5001..-79.4999 (about 11 m by 8 m), around the vehicle's start.
SQUARE_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><Polygon>
<outerBoundaryIs><LinearRing><coordinates>
-79.5001,40.4999,0 -79.4999,40.4999,0 -79.4999,40.5001,0 -79.5001,40.5001,0 -79.5001,40.4999,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></Document></kml>"""


class FixedPredictor:
    """A predictor that finds the same objects in every image."""

    def __init__(self, *detected):
        self.detected = list(detected)
        self.images = []

    def predict(self, image):
        self.images.append(image)
        return list(self.detected)


def make_detected(class_name="person", score=0.9, box=(0.4, 0.4, 0.5, 0.6)):
    """A detection with a normalized (y_min, x_min, y_max, x_max) box."""
    return Detected(class_name=class_name, score=score, box=box)


def make_frame(
    heading=0.0, pitch=-90.0, with_gimbal=True, width=FRAME_WIDTH, height=FRAME_HEIGHT
):
    pixels = np.zeros((height, width, RGB_CHANNELS), dtype=np.uint8)
    frame = frame_pb2.RawFrame(
        data=pixels.tobytes(),
        h_res=width,
        v_res=height,
        channels=RGB_CHANNELS,
        id=1,
        position_info=telemetry_pb2.PositionInfo(
            global_position=common_pb2.GlobalPosition(
                latitude=START_LAT, longitude=START_LON, heading=heading
            ),
            relative_position=common_pb2.RelativePosition(z=HEIGHT_M),
        ),
    )
    if with_gimbal:
        frame.gimbal_info.CopyFrom(
            telemetry_pb2.GimbalInfo(pose_body=common_pb2.Pose(pitch=pitch))
        )
    return frame


@pytest.fixture
def square_kml(tmp_path):
    path = tmp_path / "fence.kml"
    path.write_text(SQUARE_KML)
    return path
