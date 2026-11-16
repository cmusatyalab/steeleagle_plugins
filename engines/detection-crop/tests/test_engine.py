import dataclasses

import pytest
import uriel
from conftest import VERIFIER, make_event

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


def producer_event(kind, seq, engine=VERIFIER, outcome=uriel.Outcome.DROP):
    """An event about the producer's input `seq` at `engine`."""
    event = make_event(kind, seq, engine=engine, outcome=outcome)
    return dataclasses.replace(event, id=uriel.InputId(PRODUCER_NAME, 9, seq))


@pytest.fixture
def queue():
    return SendQueue(max_size=8)


def queued(queue, *scores):
    crops = [Crop(score=s, payload=f"crop {s}".encode(), frame_id=int(s * 100)) for s in scores]
    for c in crops:
        queue.push(c)
    return crops


def test_the_producer_is_named_after_the_engine():
    assert producer_name("crop", REINTEGRATION) == "crop.reintegration"


def settle(queue, event):
    queue.on_event(event, VERIFIER)


def test_reintegration_sends_the_queue_best_first_one_crop_at_a_time(queue):
    queued(queue, 0.2, 0.9, 0.5)
    reintegration, publish = Reintegration(queue), Publisher()
    assert reintegration.step(0, publish).score == 0.9
    # Still in the flow, however long: nothing else goes
    for kind in (K.ACCEPTED, K.SENT):
        settle(queue, producer_event(kind, 1))
    assert reintegration.step(100, publish) is None and len(publish.payloads) == 1
    # The verifier read it: the next goes
    settle(queue, producer_event(K.RESULT, 1))
    assert queue.confirmed == 1
    assert reintegration.step(100, publish).score == 0.5
    # It timed out at the verifier: its turn ended unread, so it goes again
    settle(queue, producer_event(K.TIMED_OUT, 2))
    again = reintegration.step(101, publish)
    assert again.score == 0.5 and len(again.carriers) == 2
    settle(queue, producer_event(K.RESULT, 3))
    assert reintegration.step(102, publish).score == 0.2


def test_a_better_crop_waits_for_the_one_in_flight(queue):
    (low,) = queued(queue, 0.2)
    reintegration, publish = Reintegration(queue), Publisher()
    assert reintegration.step(0, publish) is low
    (high,) = queued(queue, 0.9)
    assert reintegration.step(0, publish) is None
    settle(queue, producer_event(K.RESULT, 1))
    assert reintegration.step(0, publish) is high


def test_a_crop_dropped_on_the_way_is_sent_again(queue):
    (crop,) = queued(queue, 0.9)
    reintegration, publish = Reintegration(queue), Publisher()
    reintegration.step(0, publish)
    settle(queue, producer_event(K.RESULT, 1, engine="10.0.0.2.decoder", outcome=uriel.Outcome.DROP))
    assert reintegration.step(1, publish) is crop and crop.carriers[-1].seq == 2
