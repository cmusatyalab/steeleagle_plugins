import io
import math

import pytest
from conftest import decode
from google.protobuf.timestamp_pb2 import Timestamp
from PIL import Image

from recorder.convert import (
    detection_annotations,
    location_fix,
    message_time_ns,
    raw_frame_image,
    thumbnail_jpeg,
)
from steeleagle_protocol.v1.messages.frame.frame_pb2 import RawFrame
from steeleagle_protocol.v1.messages.result.result_pb2 import (
    BoundingBox,
    Detection,
    DetectionResult,
)
from steeleagle_protocol.v1.messages.telemetry.telemetry_pb2 import Telemetry


def test_message_time_prefers_message_timestamp():
    assert (
        message_time_ns(Timestamp(seconds=10, nanos=5), received_ns=99)
        == 10_000_000_005
    )
    assert message_time_ns(Timestamp(), received_ns=99) == 99


def test_location_fix_maps_fields():
    tel = Telemetry()
    g = tel.position_info.global_position
    g.latitude, g.longitude, g.altitude, g.heading = 40.4, -79.9, 300.0, 90.0
    v = tel.position_info.velocity_neu
    v.x_vel, v.y_vel, v.z_vel = 1.0, 2.0, 3.0  # north, east, up
    fix = decode(location_fix(tel, "veh", 1_500_000_000))
    assert (fix.latitude, fix.longitude, fix.altitude) == pytest.approx(
        (40.4, -79.9, 300.0)
    )
    assert fix.heading == pytest.approx(math.pi / 2)
    assert (fix.velocity.x, fix.velocity.y, fix.velocity.z) == (2.0, 1.0, 3.0)  # ENU
    assert fix.frame_id == "veh"
    assert (fix.timestamp.seconds, fix.timestamp.nanos) == (1, 500_000_000)


def test_location_fix_absent_or_partial():
    assert location_fix(Telemetry(), "veh", 0) is None
    tel = Telemetry()
    tel.position_info.global_position.latitude = 1.0
    tel.position_info.global_position.longitude = 2.0
    fix = decode(location_fix(tel, "veh", 0))
    assert fix.altitude == 0.0
    assert not fix.HasField("heading") and not fix.HasField("velocity")


def _size(jpeg):
    return Image.open(io.BytesIO(jpeg)).size


def test_thumbnail_keeps_aspect_and_never_upscales():
    img = Image.new("RGB", (640, 480))
    jpeg, w, h = thumbnail_jpeg(img, 320)
    assert (w, h) == (320, 240) == _size(jpeg)
    _, w, h = thumbnail_jpeg(img, 0)
    assert (w, h) == (640, 480)
    _, w, h = thumbnail_jpeg(img, 1280)
    assert (w, h) == (640, 480)


def test_raw_frame_image():
    assert (
        raw_frame_image(RawFrame(h_res=2, v_res=2, channels=3, data=b"\x00" * 5))
        is None
    )
    img = raw_frame_image(RawFrame(h_res=4, v_res=2, channels=3, data=b"\x01" * 24))
    assert img.size == (4, 2) and img.mode == "RGB"


def test_detection_annotations_scale_to_pixels():
    r = DetectionResult(
        detections=[
            Detection(
                class_name="person",
                score=0.876,
                bbox=BoundingBox(x_min=0.25, y_min=0.5, x_max=0.75, y_max=1.0),
            )
        ]
    )
    ann = decode(detection_annotations(r, 2_000_000_000, 640, 480))
    pts = [(p.x, p.y) for p in ann.points[0].points]
    assert pts == [(160, 240), (480, 240), (480, 480), (160, 480)]
    assert ann.points[0].type == 2  # LINE_LOOP
    assert ann.texts[0].text == "person 0.88"
    assert ann.points[0].timestamp.seconds == 2


def test_location_fix_needs_latitude_and_longitude():
    tel = Telemetry()
    tel.position_info.global_position.altitude = 5.0
    assert location_fix(tel, "veh", 0) is None
