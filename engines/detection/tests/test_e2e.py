"""End to end: frames go through a real Uriel producer, client, server and
engine, and the DetectionResult comes back in the producer's events."""

import threading
import time
import uuid

import pytest
import uriel
from conftest import CAMERA, FixedPredictor, make_detected, make_frame

from detection.engine import RAW_FRAME_TYPE, DETECTION_RESULT_TYPE, run
from detection.geofence import Geofence
from steeleagle_protocol.v1.messages.result import result_pb2

MAX_FRAME_BYTES = 1 << 20
MAX_OUTPUT_BYTES = 1 << 16
EVENT_TIMEOUT_S = 10.0
THREAD_JOIN_TIMEOUT_S = 10.0
FLOW_SPEC = """
[[producers]]
name = "{producer}"
tokens = 2

[[engines]]
name = "{engine}"
location = "local"

[[edges]]
from = "{producer}"
to = "{engine}"
timeout = "5s"
"""


class Deployment:
    """A producer whose frames flow to the detection engine under test."""

    def __init__(self, predictor, classes=(), geofence=None):
        suffix = uuid.uuid4().hex[:8]
        self.producer_name = f"camera-{suffix}"
        self.engine_name = f"detector-{suffix}"
        self.server = uriel.Server(f"server-{suffix}")
        self.client = uriel.Client(f"client-{suffix}", f"server-{suffix}")
        self.producer = uriel.Producer(
            self.producer_name,
            input_type=RAW_FRAME_TYPE,
            max_input_size=MAX_FRAME_BYTES,
        )
        self.events = self.client.add_producer_with_events(self.producer_name)
        self.stop = threading.Event()
        ready = threading.Event()
        self.engine_thread = threading.Thread(
            target=run,
            args=(self.engine_name, predictor, list(classes), geofence, CAMERA),
            kwargs={
                "stop": self.stop,
                "on_ready": ready.set,
                "max_output_size": MAX_OUTPUT_BYTES,
            },
            daemon=True,
        )
        self.engine_thread.start()
        assert ready.wait(EVENT_TIMEOUT_S), "engine did not start"
        self.server.add_engine(self.engine_name)
        self.client.add_flow(
            f"flow-{suffix}",
            FLOW_SPEC.format(producer=self.producer_name, engine=self.engine_name),
        )

    def detect(self, frame):
        """Publishes `frame` and returns the DetectionResult the engine reported."""
        seq = self.producer.send(frame.SerializeToString())
        deadline = time.monotonic() + EVENT_TIMEOUT_S
        while time.monotonic() < deadline:
            event = self.events.next(0.2)
            if (
                event is None
                or event.kind != uriel.EventKind.RESULT
                or event.id.seq != seq
            ):
                continue
            # No detections serialize to zero bytes, which carry no type
            assert event.payload_type in (DETECTION_RESULT_TYPE, "")
            return result_pb2.DetectionResult.FromString(event.result)
        pytest.fail("no result event for the frame")

    def close(self):
        self.stop.set()
        self.engine_thread.join(THREAD_JOIN_TIMEOUT_S)
        self.events.close()
        self.producer.close()
        self.client.close()
        self.server.close()


@pytest.fixture
def deploy():
    deployments = []

    def make(predictor, **kwargs):
        deployments.append(Deployment(predictor, **kwargs))
        return deployments[-1]

    yield make
    for deployment in deployments:
        deployment.close()


def test_a_frame_comes_back_as_a_detection_result_with_its_boxes(deploy):
    deployment = deploy(
        FixedPredictor(make_detected("person", 0.9, (0.1, 0.2, 0.6, 0.7)))
    )
    result = deployment.detect(make_frame())
    (detection,) = result.detections
    assert detection.class_name == "person"
    assert detection.bbox.x_min == pytest.approx(0.2)


def test_only_the_requested_classes_come_back(deploy):
    deployment = deploy(
        FixedPredictor(make_detected("person"), make_detected("car")), classes=["car"]
    )
    result = deployment.detect(make_frame())
    assert [d.class_name for d in result.detections] == ["car"]


def test_the_geofence_drops_detections_whose_estimated_location_is_outside(
    deploy, square_kml
):
    deployment = deploy(
        FixedPredictor(make_detected()), geofence=Geofence.from_kml(square_kml)
    )
    assert len(deployment.detect(make_frame(pitch=-90.0)).detections) == 1
    assert len(deployment.detect(make_frame(pitch=-20.0)).detections) == 0


def test_a_frame_that_cannot_be_read_still_gets_an_empty_result_and_the_engine_keeps_going(
    deploy,
):
    deployment = deploy(FixedPredictor(make_detected()))
    broken = make_frame()
    broken.channels = 4
    assert len(deployment.detect(broken).detections) == 0
    assert len(deployment.detect(make_frame()).detections) == 1
