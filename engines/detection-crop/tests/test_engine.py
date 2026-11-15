import dataclasses

import pytest
import uriel
from conftest import VERIFIER, input_id, make_event

from detection_crop.engine import REINTEGRATION, Reintegration, producer_name
from detection_crop.send_queue import Crop, SendQueue

K = uriel.EventKind
PRODUCER_NAME = producer_name("crop", REINTEGRATION)


class Publisher:
    """Stands in for the reintegration producer: records each payload."""

    def __init__(self):
        self.payloads = []

    def __call__(self, payload):
        self.payloads.append(payload)
        return uriel.InputId(PRODUCER_NAME, 9, len(self.payloads))


def producer_event(kind, seq, engine="local.encoder"):
    """An event about the producer's input `seq` at `engine`."""
    event = make_event(kind, seq, engine=engine)
    return dataclasses.replace(event, id=uriel.InputId(PRODUCER_NAME, 9, seq))


@pytest.fixture
def queue():
    return SendQueue(max_size=8, resend_after_s=5.0)


def queued(queue, *scores):
    crops = [Crop(score=s, payload=f"crop {s}".encode(), frame_id=int(s * 100)) for s in scores]
    for c in crops:
        queue.push(c)
    return crops


def test_the_producer_is_named_after_the_engine():
    assert producer_name("crop", REINTEGRATION) == "crop.reintegration"


def test_reintegration_sends_the_queue_best_first_one_admission_at_a_time(queue):
    queued(queue, 0.2, 0.9, 0.5)
    reintegration, publish = Reintegration(queue), Publisher()
    assert reintegration.step(0, publish).score == 0.9
    # Not admitted yet: publishing another would replace it at the client
    assert reintegration.step(0, publish) is None and len(publish.payloads) == 1
    # Admitted (an event at the next engine): the next goes
    reintegration.on_event(producer_event(K.ACCEPTED, 1))
    assert reintegration.step(0, publish).score == 0.5
    reintegration.on_event(producer_event(K.SENT, 2))
    assert reintegration.step(0, publish).score == 0.2
    reintegration.on_event(producer_event(K.SENT, 3))
    # All in flight: nothing to send until the verifier answers
    assert reintegration.step(0, publish) is None
    queue.on_event(producer_event(K.RESULT, 1, engine=VERIFIER))
    assert len(queue) == 2 and queue.confirmed == 1
    # The verifier timed out on 0.5: sent again
    queue.on_event(producer_event(K.TIMED_OUT, 2, engine=VERIFIER))
    assert reintegration.step(1, publish).score == 0.5


def test_a_better_crop_replaces_the_one_waiting_for_admission(queue):
    (low,) = queued(queue, 0.2)
    reintegration, publish = Reintegration(queue), Publisher()
    assert reintegration.step(0, publish) is low
    (high,) = queued(queue, 0.9)
    assert reintegration.step(0, publish) is high
    # The client replaced the low crop, so it waits again, and goes next
    assert not low.in_flight
    reintegration.on_event(producer_event(K.ACCEPTED, 2))
    assert reintegration.step(0, publish) is low


def test_a_crop_never_admitted_is_sent_again_after_resend_after(queue):
    (crop,) = queued(queue, 0.9)
    reintegration, publish = Reintegration(queue), Publisher()
    assert reintegration.step(0, publish) is crop
    assert reintegration.step(4, publish) is None
    assert reintegration.step(5, publish) is crop and crop.carriers[-1].seq == 2


def test_reintegration_does_not_send_the_crop_an_input_carries(queue):
    (crop,) = queued(queue, 0.9)
    queue.sent(crop, input_id(1), 0)
    reintegration, publish = Reintegration(queue), Publisher()
    assert reintegration.step(0, publish) is None
    # Skipped at the verifier: the producer sends it instead
    queue.on_event(make_event(K.SKIPPED, 1, engine=VERIFIER))
    assert reintegration.step(1, publish) is crop
