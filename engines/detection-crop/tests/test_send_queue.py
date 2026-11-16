import dataclasses

import pytest
import uriel
from conftest import VERIFIER, input_id, make_event

from detection_crop.send_queue import Crop, SendQueue

K = uriel.EventKind
ENCODER = "10.0.0.1.encoder"


def crop(score, frame_id=None):
    frame_id = frame_id if frame_id is not None else int(score * 100)
    return Crop(score=score, payload=b"crop", frame_id=frame_id)


def event(kind, seq=0, engine=VERIFIER, outcome=uriel.Outcome.DROP):
    return dataclasses.replace(make_event(kind, seq, engine=engine), outcome=outcome)


@pytest.fixture
def queue():
    return SendQueue(max_size=4)


def settle(queue, *events):
    for e in events:
        queue.on_event(e, VERIFIER)


def test_the_best_crop_goes_first_and_stays_until_the_verifier_has_it(queue):
    for score in (0.2, 0.9, 0.5):
        queue.push(crop(score))
    sent = queue.next(input_id(1), now=0)
    assert sent.score == 0.9
    assert len(queue) == 3 and queue.in_flight == 1
    settle(queue, event(K.RESULT, 1))
    assert len(queue) == 2 and queue.confirmed == 1
    assert queue.next(input_id(2), now=0).score == 0.5


def test_a_crop_in_flight_is_not_sent_again(queue):
    queue.push(crop(0.9))
    queue.push(crop(0.5))
    assert queue.next(input_id(1), now=0).score == 0.9
    assert queue.next(input_id(2), now=1).score == 0.5
    assert queue.next(input_id(3), now=2) is None


def test_a_yes_or_a_no_both_mean_the_verifier_read_it(queue):
    queue.push(crop(0.9))
    queue.push(crop(0.5))
    queue.next(input_id(1), now=0)
    queue.next(input_id(2), now=0)
    settle(queue, event(K.RESULT, 1, outcome=uriel.Outcome.FORWARD), event(K.RESULT, 2, outcome=uriel.Outcome.DROP))
    assert len(queue) == 0 and queue.confirmed == 2


@pytest.mark.parametrize("engine", [VERIFIER, ENCODER])
@pytest.mark.parametrize("kind", [K.SKIPPED, K.TIMED_OUT])
def test_a_turn_that_ends_unread_puts_the_crop_back(queue, kind, engine):
    queue.push(crop(0.9))
    queue.next(input_id(1), now=0)
    settle(queue, event(kind, 1, engine=engine))
    assert queue.in_flight == 0
    again = queue.next(input_id(2), now=1)
    assert again.score == 0.9 and again.carriers == [input_id(1), input_id(2)]


def test_a_crop_dropped_before_the_verifier_is_sent_again(queue):
    queue.push(crop(0.9))
    queue.next(input_id(1), now=0)
    settle(queue, event(K.RESULT, 1, engine=ENCODER, outcome=uriel.Outcome.DROP))
    assert queue.in_flight == 0 and queue.confirmed == 0


def test_a_crop_forwarded_by_an_engine_on_the_way_stays_in_flight(queue):
    queue.push(crop(0.9))
    queue.next(input_id(1), now=0)
    settle(queue, event(K.RESULT, 1, engine=ENCODER, outcome=uriel.Outcome.REPLACE),
           event(K.RESULT, 1, engine=ENCODER, outcome=uriel.Outcome.FORWARD))
    assert queue.in_flight == 1 and queue.confirmed == 0


@pytest.mark.parametrize("kind", [K.DISCONNECTED, K.LOST])
def test_a_disconnect_or_lost_events_put_every_crop_in_flight_back(queue, kind):
    queue.push(crop(0.9))
    queue.push(crop(0.5))
    queue.next(input_id(1), now=0)
    queue.next(input_id(2), now=0)
    settle(queue, event(kind))
    assert queue.in_flight == 0 and len(queue) == 2


def test_a_crop_with_no_word_stays_in_flight_however_long(queue):
    # No timer: the flow's events settle every send
    queue.push(crop(0.9))
    queue.next(input_id(1), now=0)
    assert queue.next(input_id(2), now=1e6) is None and queue.in_flight == 1


def test_a_result_of_an_earlier_carrier_still_counts(queue):
    queue.push(crop(0.9))
    queue.next(input_id(1), now=0)
    settle(queue, event(K.LOST))
    queue.next(input_id(2), now=1)
    # The first send did arrive; its RESULT was only late
    settle(queue, event(K.RESULT, 1))
    assert len(queue) == 0
    # Later events of either carrier are about nothing queued
    settle(queue, event(K.RESULT, 2))
    assert queue.confirmed == 1


def test_a_stale_skip_does_not_put_back_a_crop_resent_since(queue):
    queue.push(crop(0.9))
    queue.next(input_id(1), now=0)
    settle(queue, event(K.LOST))
    queue.next(input_id(2), now=1)
    settle(queue, event(K.SKIPPED, 1))
    assert queue.in_flight == 1


def test_accepted_and_sent_leave_a_crop_in_flight(queue):
    queue.push(crop(0.9))
    queue.next(input_id(1), now=0)
    settle(queue, event(K.ACCEPTED, 1), event(K.SENT, 1))
    assert len(queue) == 1 and queue.in_flight == 1 and queue.confirmed == 0


def test_a_remote_verifier_that_never_takes_the_crop_does_not_lose_it(queue):
    # What a busy remote verifier's events are: its server took the crop, it
    # did not
    queue.push(crop(0.9))
    queue.next(input_id(1), now=0)
    settle(queue, *(event(kind, 1) for kind in (K.ACCEPTED, K.SENT, K.TIMED_OUT)))
    assert len(queue) == 1 and queue.in_flight == 0
    assert queue.next(input_id(2), now=1).score == 0.9


def test_events_about_other_inputs_are_ignored(queue):
    queue.push(crop(0.9))
    queue.next(input_id(1), now=0)
    settle(queue, event(K.RESULT, 7), event(K.TIMED_OUT, 7))
    assert len(queue) == 1 and queue.in_flight == 1


def test_a_full_queue_drops_its_lowest_scored_crop(queue):
    for score in (0.5, 0.1, 0.7, 0.3):
        queue.push(crop(score))
    queue.next(input_id(1), now=0)  # the 0.7 crop is in flight
    dropped = queue.push(crop(0.6))
    assert dropped.score == 0.1 and len(queue) == 4
    # The new crop may be the one dropped
    assert queue.push(crop(0.05)).score == 0.05


def test_a_dropped_crop_in_flight_forgets_its_carriers():
    queue = SendQueue(max_size=1)
    queue.push(crop(0.2))
    queue.next(input_id(1), now=0)
    queue.push(crop(0.9))  # drops the 0.2 crop in flight
    settle(queue, event(K.RESULT, 1))
    assert len(queue) == 1 and queue.confirmed == 0


def test_discard_removes_a_crop(queue):
    c = crop(0.9)
    queue.push(c)
    queue.next(input_id(1), now=0)
    queue.discard(c)
    assert len(queue) == 0
    queue.discard(c)  # twice is harmless
