import io
from datetime import datetime

import pytest
import uriel
from conftest import decode_protobuf, read_mcap
from foxglove.messages import CompressedImage, CompressedVideo, ImageAnnotations
from PIL import Image
from test_h264 import IDR_STUB, P_STUB, SC, SPS_480, SPS_1080

from recorder.files import VehicleFiles
from recorder.recorder import Item, Recorder, vehicle_of
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.result import result_pb2
from steeleagle_protocol.v1.messages.telemetry import telemetry_pb2

TELEMETRY = "steeleagle_protocol/v1/messages/telemetry/Telemetry"
ENCODED = "steeleagle_protocol/v1/messages/frame/EncodedFrame"
RAW = "steeleagle_protocol/v1/messages/frame/RawFrame"
DETECTION = "steeleagle_protocol/v1/messages/result/DetectionResult"
YOLO = "10.0.0.1.yolo"
NOW = datetime(2026, 11, 5, 8, 0, 0)
FILE = "veh_2026-11-05_08-00-00.mcap"
S = 1_000_000_000
ANNOTATIONS = ImageAnnotations().get_schema()


@pytest.fixture
def setup(tmp_path):
    files = VehicleFiles(tmp_path, 60, now=lambda: NOW)
    recorder = Recorder(files, {YOLO: "yolo"}, image_width=320)

    def messages():
        files.close_all()
        path = tmp_path / FILE
        return read_mcap(path) if path.exists() else []

    return recorder, messages


def input_item(producer, payload_type, msg, seq=0, received_ns=0):
    return Item(
        "input",
        uriel.InputId(producer, 1, seq),
        payload_type,
        msg.SerializeToString(),
        "",
        received_ns,
    )


def result_item(producer, seq, data, payload_type=DETECTION, received_ns=0):
    return Item(
        "event", uriel.InputId(producer, 1, seq), payload_type, data, YOLO, received_ns
    )


def h264(seq, *nals, ts_s=0, camera=0):
    data = b"".join(SC + n for n in nals)
    frame = frame_pb2.EncodedFrame(
        encoded_data=data, codec=frame_pb2.CODEC_H264, camera_id=camera, id=seq
    )
    if ts_s:
        frame.timestamp.seconds = ts_s
    return frame


def detection():
    return result_pb2.DetectionResult(
        detections=[
            result_pb2.Detection(
                class_name="car",
                score=0.5,
                bbox=result_pb2.BoundingBox(x_min=0.5, y_min=0.5, x_max=1.0, y_max=1.0),
            )
        ]
    ).SerializeToString()


def annotation_points(msg):
    ann = decode_protobuf(ANNOTATIONS.name, bytes(ANNOTATIONS.data), msg[3])
    return [(p.x, p.y) for p in ann.points[0].points]


def topics(msgs):
    return [m[0] for m in msgs]


def test_vehicle_of():
    assert vehicle_of("100.64.0.1.canary.telemetry") == "100.64.0.1.canary"
    assert vehicle_of("telemetry") is None


def test_telemetry_logged_twice(setup):
    recorder, messages = setup
    tel = telemetry_pb2.Telemetry()
    tel.timestamp.seconds = 7
    tel.position_info.global_position.latitude = 1.0
    tel.position_info.global_position.longitude = 2.0
    recorder.handle(input_item("veh.telemetry", TELEMETRY, tel))
    msgs = messages()
    assert sorted(topics(msgs)) == ["/veh/location", "/veh/telemetry"]
    assert {m[2] for m in msgs} == {7 * S}
    assert [m[3] for m in msgs if m[0] == "/veh/telemetry"] == [tel.SerializeToString()]


def test_producer_without_vehicle_skipped(setup, tmp_path):
    recorder, messages = setup
    recorder.handle(input_item("telemetry", TELEMETRY, telemetry_pb2.Telemetry()))
    assert list(tmp_path.iterdir()) == []


def test_h264_waits_for_sps_then_records_all(setup):
    recorder, messages = setup
    frames = [
        h264(0, P_STUB, ts_s=1),
        h264(1, P_STUB, ts_s=2),
        h264(2, SPS_480, IDR_STUB, ts_s=3),
        h264(3, P_STUB, ts_s=4),
    ]
    for i, f in enumerate(frames):
        recorder.handle(input_item("veh.cam", ENCODED, f, seq=i))
    msgs = messages()
    assert topics(msgs) == ["/veh/camera/0", "/veh/camera/0"]
    assert msgs[0][1] == "foxglove.CompressedVideo"
    s = CompressedVideo().get_schema()
    decoded = [decode_protobuf(s.name, bytes(s.data), m[3]) for m in msgs]
    assert [d.data for d in decoded] == [frames[2].encoded_data, frames[3].encoded_data]
    assert decoded[0].format == "h264"
    assert msgs[0][2] == 3 * S


def _jpeg(w, h):
    out = io.BytesIO()
    Image.new("RGB", (w, h), (200, 10, 10)).save(out, format="JPEG")
    return out.getvalue()


def _stored_size(msg):
    s = CompressedImage().get_schema()
    img = decode_protobuf(s.name, bytes(s.data), msg[3])
    assert img.format == "jpeg"
    return Image.open(io.BytesIO(img.data)).size


def test_jpeg_and_raw_frames_resized(setup):
    recorder, messages = setup
    jpeg = frame_pb2.EncodedFrame(
        encoded_data=_jpeg(640, 480), codec=frame_pb2.CODEC_JPEG, camera_id=1
    )
    raw = frame_pb2.RawFrame(
        h_res=640, v_res=480, channels=3, data=b"\x10" * (640 * 480 * 3), camera_id=2
    )
    bad = frame_pb2.RawFrame(
        h_res=640, v_res=480, channels=3, data=b"\x10", camera_id=3
    )
    recorder.handle(input_item("veh.a", ENCODED, jpeg, seq=0))
    recorder.handle(input_item("veh.b", RAW, raw, seq=0))
    recorder.handle(input_item("veh.c", RAW, bad, seq=0))
    msgs = messages()
    assert sorted(topics(msgs)) == ["/veh/camera/1", "/veh/camera/2"]
    assert [_stored_size(m) for m in msgs] == [(320, 240), (320, 240)]


def test_detection_overlay_uses_frame_time_and_size(setup):
    recorder, messages = setup
    recorder.handle(
        input_item("veh.cam", ENCODED, h264(5, SPS_480, IDR_STUB, ts_s=9), seq=5)
    )
    recorder.handle(result_item("veh.cam", 5, detection(), received_ns=1 * S))
    msgs = messages()
    ann = [m for m in msgs if m[0] == "/veh/camera/0/detections/yolo"]
    assert len(ann) == 1
    assert ann[0][2] == 9 * S
    assert annotation_points(ann[0]) == [(320, 240), (640, 240), (640, 480), (320, 480)]
    results = [m for m in msgs if m[0] == "/veh/results/yolo"]
    assert [(r[1], r[3]) for r in results] == [
        ("steeleagle_protocol.v1.messages.result.DetectionResult", detection())
    ]


def test_annotations_follow_new_sps_size(setup):
    recorder, messages = setup
    recorder.handle(
        input_item("veh.cam", ENCODED, h264(0, SPS_480, IDR_STUB, ts_s=1), seq=0)
    )
    recorder.handle(
        input_item("veh.cam", ENCODED, h264(1, SPS_1080, IDR_STUB, ts_s=2), seq=1)
    )
    recorder.handle(input_item("veh.cam", ENCODED, h264(2, P_STUB, ts_s=3), seq=2))
    recorder.handle(result_item("veh.cam", 2, detection()))
    ann = [m for m in messages() if m[0] == "/veh/camera/0/detections/yolo"]
    assert annotation_points(ann[0])[2] == (1920, 1080)


def test_raw_frame_annotations_use_stored_size(setup):
    recorder, messages = setup
    raw = frame_pb2.RawFrame(
        h_res=640, v_res=480, channels=3, data=b"\x10" * (640 * 480 * 3)
    )
    recorder.handle(input_item("veh.cam", RAW, raw, seq=0, received_ns=4 * S))
    recorder.handle(result_item("veh.cam", 0, detection(), received_ns=4 * S))
    ann = [m for m in messages() if m[0] == "/veh/camera/0/detections/yolo"]
    assert annotation_points(ann[0])[2] == (320, 240)
    assert ann[0][2] == 4 * S


def test_result_without_frame_logged_only_as_protobuf(setup):
    recorder, messages = setup
    recorder.handle(result_item("veh.cam", 3, detection()))
    assert topics(messages()) == ["/veh/results/yolo"]


def test_unknown_result_type_logged_raw(setup):
    recorder, messages = setup
    recorder.handle(result_item("veh.cam", 3, b"\x01", payload_type="other/Thing"))
    msgs = messages()
    assert topics(msgs) == ["/veh/results/yolo"]
    assert msgs[0][4] == "json"


def test_malformed_detection_skipped(setup):
    recorder, messages = setup
    recorder.handle(
        input_item("veh.cam", ENCODED, h264(0, SPS_480, IDR_STUB, ts_s=1), seq=0)
    )
    recorder.handle(result_item("veh.cam", 0, b"\xff\xff"))
    assert "/veh/camera/0/detections/yolo" not in topics(messages())


def test_frame_memory_evicts_old_frames(setup):
    recorder, messages = setup
    recorder.handle(
        input_item("veh.cam", ENCODED, h264(0, SPS_480, IDR_STUB, ts_s=1), seq=0)
    )
    recorder.handle(
        input_item(
            "veh.cam", ENCODED, h264(1, P_STUB, ts_s=7), seq=1, received_ns=6 * S
        )
    )
    recorder.handle(result_item("veh.cam", 0, detection(), received_ns=6 * S))
    assert "/veh/camera/0/detections/yolo" not in topics(messages())


def test_ignores_other_events_and_engines(setup, tmp_path):
    recorder, messages = setup
    sent = Item("event", uriel.InputId("veh.cam", 1, 0), "", b"", YOLO, 0)
    other = Item(
        "event",
        uriel.InputId("veh.cam", 1, 0),
        DETECTION,
        detection(),
        "10.0.0.1.other",
        0,
    )
    recorder.handle(sent)
    recorder.handle(other)
    assert list(tmp_path.iterdir()) == []


def test_detection_before_its_frame_is_drawn_when_the_frame_arrives(setup):
    recorder, messages = setup
    recorder.handle(result_item("veh.cam", 5, detection(), received_ns=1 * S))
    recorder.handle(
        input_item(
            "veh.cam",
            ENCODED,
            h264(5, SPS_480, IDR_STUB, ts_s=9),
            seq=5,
            received_ns=1 * S,
        )
    )
    ann = [m for m in messages() if m[0] == "/veh/camera/0/detections/yolo"]
    assert len(ann) == 1 and ann[0][2] == 9 * S
    assert annotation_points(ann[0])[2] == (640, 480)


def test_early_detection_expires(setup):
    recorder, messages = setup
    recorder.handle(result_item("veh.cam", 5, detection(), received_ns=0))
    recorder.handle(
        input_item(
            "veh.cam",
            ENCODED,
            h264(5, SPS_480, IDR_STUB, ts_s=9),
            seq=5,
            received_ns=6 * S,
        )
    )
    assert "/veh/camera/0/detections/yolo" not in topics(messages())


def test_dotted_payload_types_are_recognised(setup):
    recorder, messages = setup
    recorder.handle(
        input_item(
            "veh.cam",
            ENCODED.replace("/", "."),
            h264(5, SPS_480, IDR_STUB, ts_s=9),
            seq=5,
        )
    )
    recorder.handle(
        result_item("veh.cam", 5, detection(), payload_type=DETECTION.replace("/", "."))
    )
    msgs = messages()
    assert "/veh/camera/0/detections/yolo" in topics(msgs)
    assert [m[4] for m in msgs if m[0] == "/veh/results/yolo"] == ["protobuf"]


def test_new_recording_waits_for_a_keyframe_again(setup, tmp_path):
    recorder, messages = setup
    recorder.handle(
        input_item("veh.cam", ENCODED, h264(0, SPS_480, IDR_STUB, ts_s=1), seq=0)
    )
    recorder.files.close_all()
    recorder.forget("veh")
    recorder.handle(input_item("veh.cam", ENCODED, h264(1, P_STUB, ts_s=2), seq=1))
    recorder.handle(
        input_item("veh.cam", ENCODED, h264(2, SPS_480, IDR_STUB, ts_s=3), seq=2)
    )
    recorder.files.close_all()
    second = tmp_path / "veh_2026-11-05_08-00-00_2.mcap"
    assert [m[2] for m in read_mcap(second)] == [3 * S]
