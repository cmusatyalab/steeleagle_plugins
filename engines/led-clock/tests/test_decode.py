from pathlib import Path

import cv2
import numpy as np
import pytest
from conftest import (
    BACKGROUND,
    LEFT_MARKER,
    RIGHT_MARKER,
    YELLOW,
    bit_x,
    make_image,
    to_gray,
)

from led_clock.decode import (
    BITS,
    LedReader,
    display_time_ns,
    find_markers,
    gray_to_binary,
    led_positions,
    patch_radius,
    sample_leds,
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
    assert np.allclose(right, RIGHT_MARKER, atol=MARKER_TOLERANCE_PX)


def test_no_markers_are_found_without_yellow():
    image = make_image(ALL_ON)
    image[np.all(image == YELLOW, axis=-1)] = BACKGROUND
    assert find_markers(image) is None


def test_lit_red_leds_are_not_markers():
    left, right = find_markers(make_image(ALL_ON))
    assert np.allclose(left, LEFT_MARKER, atol=MARKER_TOLERANCE_PX)


def test_leds_lie_at_their_breadboard_rows_with_bit_0_leftmost():
    positions = led_positions(np.array(LEFT_MARKER, float), np.array(RIGHT_MARKER, float))
    assert positions.shape == (BITS, 2)
    for bit in range(BITS):
        assert positions[bit] == pytest.approx((bit_x(bit), LEFT_MARKER[1]))


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


# A snapshot from the Anafi's live stream, as received on lambda2 (the
# engine's rings and labels are drawn on it, away from the LEDs' centers)
ANAFI_SNAPSHOT = Path(__file__).parent / "data" / "anafi_snapshot.jpeg"
# Which LEDs are lit in it, bit 0 (the leftmost) first, as seen by eye
ANAFI_SNAPSHOT_LIT = np.array([1, 1, 1, 0, 1, 0, 1, 1, 0, 1], dtype=bool)
# Least gap between the dimmest lit LED and the brightest unlit one
MIN_SEPARATION = 50


def test_lit_and_unlit_leds_separate_in_a_real_frame():
    rgb = cv2.cvtColor(cv2.imread(str(ANAFI_SNAPSHOT)), cv2.COLOR_BGR2RGB)
    left, right = find_markers(rgb)
    levels = sample_leds(rgb, led_positions(left, right), patch_radius(left, right))
    assert levels[ANAFI_SNAPSHOT_LIT].min() - levels[~ANAFI_SNAPSHOT_LIT].max() >= MIN_SEPARATION
