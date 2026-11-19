"""End to end: frames go through a real Uriel producer, client, server and
engine, and the DetectionResult comes back in the producer's events."""

import threading
import time
import uuid

import pytest
import uriel
from conftest import make_frame
from steeleagle_protocol.v1.messages.result import result_pb2

from aruco.engine import DETECTION_RESULT_TYPE, RAW_FRAME_TYPE, make_detector, run

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
    """A producer whose frames flow to the ArUco engine under test."""

    def __init__(self, only_ids=(), early_discard=False):
        suffix = uuid.uuid4().hex[:8]
        self.producer_name = f"camera-{suffix}"
        self.engine_name = f"aruco-{suffix}"
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
            args=(
                self.engine_name,
                make_detector("DICT_ARUCO_ORIGINAL"),
                list(only_ids),
            ),
            kwargs={
                "stop": self.stop,
                "on_ready": ready.set,
                "max_output_size": MAX_OUTPUT_BYTES,
                "early_discard": early_discard,
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

    def detect_event(self, frame):
        """Publishes `frame` and returns the engine's RESULT event for it,
        with the DetectionResult it reported."""
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
            return event, result_pb2.DetectionResult.FromString(event.result)
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

    def make(**kwargs):
        deployments.append(Deployment(**kwargs))
        return deployments[-1]

    yield make
    for deployment in deployments:
        deployment.close()


def test_a_frame_comes_back_with_its_markers(deploy):
    event, result = deploy().detect_event(make_frame([(0, 100, 50)]))
    assert [d.class_name for d in result.detections] == ["aruco_0"]
    assert event.outcome == uriel.Outcome.FORWARD


def test_a_frame_without_markers_is_forwarded_with_an_empty_result(deploy):
    event, result = deploy().detect_event(make_frame())
    assert len(result.detections) == 0
    assert event.outcome == uriel.Outcome.FORWARD


def test_with_early_discard_only_frames_with_markers_are_forwarded(deploy):
    deployment = deploy(early_discard=True)
    event, _ = deployment.detect_event(make_frame())
    assert event.outcome == uriel.Outcome.DROP
    event, result = deployment.detect_event(make_frame([(0, 100, 50)]))
    assert len(result.detections) == 1
    assert event.outcome == uriel.Outcome.FORWARD


def test_a_bad_frame_gets_an_empty_result_and_the_next_one_is_served(deploy):
    deployment = deploy()
    bad = make_frame()
    bad.h_res += 1
    _, result = deployment.detect_event(bad)
    assert len(result.detections) == 0
    _, result = deployment.detect_event(make_frame([(4, 100, 50)]))
    assert [d.class_name for d in result.detections] == ["aruco_4"]
