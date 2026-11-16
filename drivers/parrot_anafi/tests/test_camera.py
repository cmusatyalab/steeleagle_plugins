"""Tests of setting the camera's exposure and HDR from the driver's args.

These need no drone: a fake one records the commands sent to it and answers
each with the result the test gives it.
"""
import pytest
from olympe.enums.camera import exposure_mode, shutter_speed, state

from parrot_anafi.camera import CameraConfigError, configure_camera, parse_shutter_speed


class FakeResult:
    def __init__(self, ok):
        self._ok = ok

    def success(self):
        return self._ok


class FakeDrone:
    """Records each command sent, and answers them all with `ok`."""

    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def __call__(self, expectation):
        command = expectation.expectations[0]
        self.sent.append((command.command_message.name, command.command_args))
        return self

    def wait(self, _timeout):
        return FakeResult(self.ok)


@pytest.mark.parametrize('text, speed', [
    ('1/500', shutter_speed.shutter_1_over_500),
    ('1/10000', shutter_speed.shutter_1_over_10000),
    ('1/1.5', shutter_speed.shutter_1_over_1_5),
    ('1', shutter_speed.shutter_1),
])
def test_shutter_speeds_are_written_as_fractions_of_a_second(text, speed):
    assert parse_shutter_speed(text) == speed


@pytest.mark.parametrize('text', ['1/499', '500', '1_over_500', ''])
def test_shutter_speeds_the_camera_lacks_are_refused(text):
    with pytest.raises(ValueError, match='1/500'):
        parse_shutter_speed(text)


def test_nothing_is_sent_unless_asked():
    drone = FakeDrone()
    configure_camera(drone)
    assert drone.sent == []


def test_a_shutter_speed_fixes_the_shutter_and_leaves_iso_automatic():
    drone = FakeDrone()
    configure_camera(drone, shutter_speed='1/500')
    [(name, args)] = drone.sent
    assert name == 'set_exposure_settings'
    assert args['mode'] == exposure_mode.manual_shutter_speed
    assert args['shutter_speed'] == shutter_speed.shutter_1_over_500


def test_auto_restores_automatic_exposure():
    drone = FakeDrone()
    configure_camera(drone, shutter_speed='auto')
    [(name, args)] = drone.sent
    assert name == 'set_exposure_settings'
    assert args['mode'] == exposure_mode.automatic


@pytest.mark.parametrize('hdr, value', [(False, state.inactive), (True, state.active)])
def test_hdr_is_turned_on_or_off(hdr, value):
    drone = FakeDrone()
    configure_camera(drone, hdr=hdr)
    assert drone.sent == [('set_hdr_setting', {'cam_id': 0, 'value': value})]


@pytest.mark.parametrize('kwargs', [{'shutter_speed': '1/500'}, {'hdr': False}])
def test_a_setting_the_drone_does_not_take_is_an_error(kwargs):
    with pytest.raises(CameraConfigError):
        configure_camera(FakeDrone(ok=False), **kwargs)
