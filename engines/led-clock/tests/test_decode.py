import numpy as np
import pytest
from conftest import (
    BACKGROUND,
    LEFT_MARKER,
    RIGHT_MARKER_ROW,
    YELLOW,
    make_image,
    row_x,
    to_gray,
)

from led_clock.decode import (
    BITS,
    LedReader,
    display_time_ns,
    find_markers,
    gray_to_binary,
    led_positions,
)

TICK_NS = 5_000_000
ALL_ON = (1 << BITS) - 1
MARKER_TOLERANCE_PX = 1.0


def primed_reader():
    """A reader that has seen every LED both on and off."""
    reader = LedReader()
    reader.read(make_image(ALL_ON))
    reader.read(make_image(0))
    return reader


def test_gray_to_binary_inverts_gray_code():
    assert [gray_to_binary(to_gray(n)) for n in range(1 << BITS)] == list(range(1 << BITS))


def test_markers_are_found_left_to_right():
    left, right = find_markers(make_image(0))
    assert np.allclose(left, LEFT_MARKER, atol=MARKER_TOLERANCE_PX)
    assert np.allclose(right, (row_x(RIGHT_MARKER_ROW), LEFT_MARKER[1]), atol=MARKER_TOLERANCE_PX)


def test_no_markers_are_found_without_yellow():
    image = make_image(ALL_ON)
    image[np.all(image == YELLOW, axis=-1)] = BACKGROUND
    assert find_markers(image) is None


def test_lit_red_leds_are_not_markers():
    left, right = find_markers(make_image(ALL_ON))
    assert np.allclose(left, LEFT_MARKER, atol=MARKER_TOLERANCE_PX)


def test_leds_lie_at_their_breadboard_rows_between_the_markers():
    left = np.array(LEFT_MARKER, dtype=float)
    right = np.array((row_x(RIGHT_MARKER_ROW), LEFT_MARKER[1]), dtype=float)
    positions = led_positions(left, right)
    assert positions.shape == (BITS, 2)
    assert positions[0] == pytest.approx((row_x(54), LEFT_MARKER[1]))
    assert positions[9] == pytest.approx((row_x(9), LEFT_MARKER[1]))


def test_nothing_is_decoded_before_each_led_is_seen_on_and_off():
    reader = LedReader()
    assert reader.read(make_image(ALL_ON)).code is None


@pytest.mark.parametrize("tick", [0, 1, 341, 682, 1000, 1023])
def test_the_tick_is_decoded(tick):
    reading = primed_reader().read(make_image(to_gray(tick)))
    assert reading.code == tick
    assert reading.confidence > 0.5


def test_a_shifted_frame_is_decoded():
    reading = primed_reader().read(make_image(to_gray(777), offset=(-40, 25)))
    assert reading.code == 777


def test_a_noisy_frame_is_decoded():
    reader = primed_reader()
    for seed in range(5):
        assert reader.read(make_image(to_gray(555), noise=15, seed=seed)).code == 555


def test_a_frame_without_markers_has_no_code():
    reading = primed_reader().read(np.zeros_like(make_image(0)))
    assert reading.markers is None
    assert reading.code is None


def test_the_display_time_is_the_middle_of_the_latest_matching_tick():
    recv_ns = 1_000_000 * TICK_NS + 1_234
    assert display_time_ns(1_000_000 % 1024, recv_ns, TICK_NS) == 1_000_000 * TICK_NS + TICK_NS // 2
    # A code just past the receive tick was shown one wrap earlier
    code = (1_000_000 + 1) % 1024
    expected_tick = 1_000_000 + 1 - 1024
    assert display_time_ns(code, recv_ns, TICK_NS) == expected_tick * TICK_NS + TICK_NS // 2
