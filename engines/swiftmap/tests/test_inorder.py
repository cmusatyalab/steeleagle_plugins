"""Tests of putting frames back in order."""

from swiftmap.inorder import InOrder, runs

TIMEOUT = 30.0
RETRY = 10.0


def inorder(**kwargs):
    return InOrder(TIMEOUT, retry_interval=RETRY, **kwargs)


def add_all(order, numbers, now=0.0):
    ready = []
    for n in numbers:
        ready += order.add(n, f"f{n}", now)
    return ready


def frames(*numbers):
    return [f"f{n}" for n in numbers]


def test_runs_of_consecutive_numbers():
    assert runs([]) == []
    assert runs([4, 5, 7, 9, 10, 11]) == [(4, 5), (7, 7), (9, 11)]


def test_frames_in_order_are_released_as_they_come():
    order = inorder()
    assert add_all(order, [5, 6, 7]) == frames(5, 6, 7)
    assert order.requests(0.0) == []


def test_frames_after_a_gap_wait_for_the_missing_ones_in_any_order():
    order = inorder()
    assert add_all(order, [1, 2, 3, 6, 7, 8]) == frames(1, 2, 3)
    assert sorted(order.missing) == [4, 5]
    assert order.fill(5, "f5") == []
    assert order.fill(4, "f4") == frames(4, 5, 6, 7, 8)
    assert order.recovered == 2
    assert add_all(order, [9]) == frames(9)


def test_only_missing_frames_are_filled():
    order = inorder()
    add_all(order, [1, 3])
    assert order.fill(1, "again") == []
    assert order.fill(7, "f7") == []
    assert order.fill(2, "f2") == frames(2, 3)
    assert order.fill(2, "again") == []


def test_a_gap_is_asked_for_at_once_then_every_retry_interval():
    order = inorder()
    add_all(order, [1, 4, 5, 8])
    assert order.requests(0.0) == [(2, 3), (6, 7)]
    assert order.requests(RETRY - 1) == []
    order.fill(2, "f2")
    assert order.requests(RETRY) == [(3, 3), (6, 7)]
    # A new gap is asked for at once
    add_all(order, [10], now=RETRY + 1)
    assert order.requests(RETRY + 1) == [(3, 3), (6, 7), (9, 9)]


def test_frames_missing_for_gap_timeout_are_given_up_on():
    order = inorder()
    add_all(order, [1, 3], now=0.0)
    add_all(order, [5], now=10.0)
    assert order.expire(TIMEOUT - 1) == []
    # 2 went missing at 0, 4 at 10
    assert order.expire(TIMEOUT) == frames(3)
    assert order.expire(TIMEOUT + 10) == frames(5)
    assert order.given_up == 2
    assert order.fill(2, "late") == []


def test_holding_more_than_max_held_gives_up_on_the_missing_frames():
    order = inorder(max_held=3)
    assert add_all(order, [1, 3, 4, 5]) == frames(1)
    assert add_all(order, [6]) == frames(3, 4, 5, 6)
    assert order.missing == {}


def test_numbers_starting_over_release_the_frames_held_first():
    order = inorder()
    assert add_all(order, [7, 9, 10]) == frames(7)
    assert add_all(order, [0, 1]) == frames(9, 10, 0, 1)
    assert order.missing == {}


def test_flush_releases_everything_and_forgets_the_stream():
    order = inorder()
    add_all(order, [1, 3, 5])
    assert order.flush() == frames(3, 5)
    assert add_all(order, [100]) == frames(100)
