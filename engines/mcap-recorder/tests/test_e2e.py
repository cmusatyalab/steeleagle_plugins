"""End to end: telemetry and H.264 frames go through a real uriel producer,
client and server to the recorder, and the frames also to a stand-in
detector whose results the recorder subscribes to."""

import threading
import time
import uuid

import uriel
from conftest import read_mcap
from test_h264 import IDR_STUB, P_STUB, SC, SPS_480

from recorder.engine import run
from recorder.files import VehicleFiles
from recorder.recorder import Recorder
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.result import result_pb2
from steeleagle_protocol.v1.messages.telemetry import telemetry_pb2

MAX_BYTES = 1 << 16
TIMEOUT_S = 10.0
TELEMETRY_TYPE = "steeleagle_protocol/v1/messages/telemetry/Telemetry"
ENCODED_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/EncodedFrame"
DETECTION_TYPE = "steeleagle_protocol/v1/messages/result/DetectionResult"
FLOW_SPEC = """
[[producers]]
name = "{cam}"
tokens = 4

[[producers]]
name = "{tel}"
tokens = 4

[[engines]]
name = "{recorder}"
location = "local"

[[engines]]
name = "{detector}"
location = "local"

[[edges]]
from = "{cam}"
to = "{recorder}"
timeout = "5s"

[[edges]]
from = "{cam}"
to = "{detector}"
timeout = "5s"

[[edges]]
from = "{tel}"
to = "{recorder}"
timeout = "5s"
"""


def serve_detector(name, stop, ready):
    """Drops each frame with a one-box DetectionResult as its result."""
    result = result_pb2.DetectionResult(
        detections=[
            result_pb2.Detection(
                class_name="car",
                score=0.9,
                bbox=result_pb2.BoundingBox(x_min=0.1, y_min=0.1, x_max=0.2, y_max=0.2),
            )
        ]
    ).SerializeToString()
    with uriel.Engine(
        name, result_type=DETECTION_TYPE, max_output_size=MAX_BYTES
    ) as engine:
        ready.set()
        while not stop.is_set():
            if engine.next(0.05) is not None:
                engine.drop(result=result)


def frame(seq, *nals):
    f = frame_pb2.EncodedFrame(
        encoded_data=b"".join(SC + n for n in nals), codec=frame_pb2.CODEC_H264, id=seq
    )
    f.timestamp.seconds = 1000 + seq
    return f.SerializeToString()


def test_records_telemetry_frames_and_detections(tmp_path):
    suffix = uuid.uuid4().hex[:8]
    vehicle = f"veh{suffix}"
    cam, tel = f"{vehicle}.cam", f"{vehicle}.telemetry"
    recorder_name, detector_name = f"recorder-{suffix}", f"yolo-{suffix}"
    stop = threading.Event()
    recorder_ready, detector_ready = threading.Event(), threading.Event()

    detector = threading.Thread(
        target=serve_detector, args=(detector_name, stop, detector_ready), daemon=True
    )
    detector.start()
    assert detector_ready.wait(TIMEOUT_S)
    files = VehicleFiles(tmp_path, 60)
    recorder = Recorder(files, {detector_name: "yolo"}, image_width=320)
    rec = threading.Thread(
        target=run,
        args=(recorder_name, recorder, files, [detector_name]),
        kwargs={"stop": stop, "on_ready": recorder_ready.set},
        daemon=True,
    )
    rec.start()
    assert recorder_ready.wait(TIMEOUT_S)

    try:
        with (
            uriel.Server(f"server-{suffix}") as server,
            uriel.Client(f"client-{suffix}", f"server-{suffix}") as client,
            uriel.Producer(
                cam, input_type=ENCODED_FRAME_TYPE, max_input_size=MAX_BYTES
            ) as cam_p,
            uriel.Producer(
                tel, input_type=TELEMETRY_TYPE, max_input_size=MAX_BYTES
            ) as tel_p,
        ):
            server.add_engine(recorder_name)
            server.add_engine(detector_name)
            client.add_producer(cam)
            client.add_producer(tel)
            client.add_flow(
                f"flow-{suffix}",
                FLOW_SPEC.format(
                    cam=cam, tel=tel, recorder=recorder_name, detector=detector_name
                ),
            )
            t = telemetry_pb2.Telemetry()
            t.position_info.global_position.latitude = 40.0
            t.position_info.global_position.longitude = -80.0
            tel_p.send(t.SerializeToString())
            for seq, nals in enumerate([(SPS_480, IDR_STUB), (P_STUB,), (P_STUB,)]):
                cam_p.send(frame(seq, *nals))
                time.sleep(0.1)
            time.sleep(1.0)
    finally:
        stop.set()
        rec.join(TIMEOUT_S)
        detector.join(TIMEOUT_S)

    paths = list(tmp_path.iterdir())
    assert len(paths) == 1 and paths[0].name.startswith(f"{vehicle}_")
    counts = {}
    for topic, *_ in read_mcap(paths[0]):
        counts[topic] = counts.get(topic, 0) + 1
    assert counts == {
        f"/{vehicle}/telemetry": 1,
        f"/{vehicle}/location": 1,
        f"/{vehicle}/camera/0": 3,
        f"/{vehicle}/camera/0/detections/yolo": 3,
        f"/{vehicle}/results/yolo": 3,
    }
