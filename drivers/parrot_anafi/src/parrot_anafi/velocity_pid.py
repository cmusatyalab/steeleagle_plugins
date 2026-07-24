"""Velocity control loop for the Parrot Anafi.

Olympe does not expose a native "set velocity" command like MAVLink vehicles
do. It only exposes a virtual joystick (PCMD: pitch/roll tilt, yaw rate, and
thrust). VelocityPIDThread reads the drone's IMU-derived velocity, runs a PID
controller per axis, and converts the result into PCMD stick values so that
SetVelocity requests behave like a real velocity setpoint.

The thread runs for the lifetime of the driver and is paused/resumed by
whichever flight mode is currently active, rather than being spawned and
cancelled per-request.
"""

import threading
import time
from dataclasses import dataclass
from math import radians, cos as mcos, sin as msin

import numpy as np

from olympe.messages.ardrone3.Piloting import PCMD
from olympe.messages.ardrone3.PilotingState import AttitudeChanged, SpeedChanged
from olympe.messages.ardrone3.SpeedSettingsState import MaxRotationSpeedChanged

import steeleagle_protocol.v1.common_pb2 as common_proto
import steeleagle_protocol.v1.services.driver.control_pb2 as control_proto

# PCMD stick range accepted by Olympe.
PCMD_MIN = -100
PCMD_MAX = 100

# Loop rate for the control thread.
LOOP_INTERVAL_S = 0.1
# If more than this much time passes between samples (e.g. after being paused),
# treat the next sample as the first one instead of computing a
# derivative/integral across the gap.
STALE_SAMPLE_S = 1.0

# Velocity errors smaller than this are treated as "at setpoint" and zeroed out
# to avoid chasing sensor noise.
ERROR_DEADBAND_MPS = 0.1
# Below this error magnitude the proportional term is halved (and the integral
# term dropped) so the drone settles instead of oscillating.
SETTLING_ERROR_MPS = 0.5
# Below this error magnitude the integral term is dropped entirely.
INTEGRAL_DEADBAND_MPS = 0.05


@dataclass(frozen=True)
class PIDGains:
    """Tunable gains for a single-axis velocity PID controller."""
    kp: float
    ki: float
    kd: float
    max_i: float


# Default gains, tuned for the Anafi/Anafi USA airframes. Callers can override
# these per-axis after constructing VelocityPIDThread.
DEFAULT_FORWARD_GAINS = PIDGains(kp=0.3, ki=0.001, kd=100.0, max_i=10.0)
DEFAULT_RIGHT_GAINS = PIDGains(kp=0.3, ki=0.001, kd=100.0, max_i=10.0)
DEFAULT_UP_GAINS = PIDGains(kp=2.0, ki=0.0, kd=100.0, max_i=10.0)


def _clamp(value, lo, hi):
    return max(lo, min(value, hi))


def get_velocity_neu(drone):
    """Reads the drone's current velocity in the NEU (North/East/Up) frame."""
    speed = drone.get_state(SpeedChanged)
    velocity = common_proto.Velocity()
    velocity.x_vel = speed['speedX']
    velocity.y_vel = speed['speedY']
    velocity.z_vel = speed['speedZ'] * -1
    return velocity


def get_velocity_body(drone):
    """Reads the drone's current velocity in its own body frame (forward/right/up)."""
    neu = get_velocity_neu(drone)
    heading = drone.get_state(AttitudeChanged)['yaw']

    forward_hdg = radians(heading) + radians(90)
    forward_axis = np.array([mcos(forward_hdg), msin(forward_hdg)])
    right_axis = np.array([mcos(forward_hdg + radians(90)), msin(forward_hdg + radians(90))])
    ground_vec = np.array([neu.x_vel, neu.y_vel])

    velocity = common_proto.Velocity()
    velocity.x_vel = np.dot(ground_vec, forward_axis) * -1
    velocity.y_vel = np.dot(ground_vec, right_axis) * -1
    velocity.z_vel = neu.z_vel
    return velocity


class AxisPID:
    """A single-axis PID controller with settling/deadband shaping.

    This intentionally mirrors "textbook" PID plus two shaping rules that
    matter for a physical vehicle actuated over a lossy virtual joystick: small
    errors are deadbanded to reject sensor noise, and the integral term is
    suppressed near the setpoint so the drone doesn't overshoot while settling.
    """

    def __init__(self, gains: PIDGains):
        self._gains = gains
        self._integral = 0.0
        self._prev_error = 0.0
        self._prev_time = None

    def reset(self):
        """Clears accumulated integral/derivative state (e.g. on pause or braking)."""
        self._integral = 0.0
        self._prev_error = 0.0
        self._prev_time = None

    def update(self, error: float, now: float) -> float:
        """Advances the controller by one sample and returns the control output."""
        if abs(error) < ERROR_DEADBAND_MPS:
            error = 0.0

        if self._prev_time is None or (now - self._prev_time) > STALE_SAMPLE_S:
            self._prev_time = now
            self._prev_error = error

        dt = now - self._prev_time

        p_term = self._gains.kp * error

        if abs(error) < SETTLING_ERROR_MPS:
            # Near the setpoint: damp the proportional response and freeze
            # the integral rather than let it keep winding.
            self._prev_time = now
            self._prev_error = error
            return p_term / 2.0

        i_term = 0.0
        if abs(error) > INTEGRAL_DEADBAND_MPS and dt > 0:
            i_term = self._gains.ki * dt
            i_term = i_term if error >= 0.0 else -i_term
            # Don't wind up further once the opposite sign is already applied.
            if i_term * self._integral < 0:
                i_term = 0.0
        self._integral = _clamp(self._integral + i_term, -self._gains.max_i, self._gains.max_i)

        d_term = 0.0
        if abs(error) > 0.01 and dt > 0:
            d_term = self._gains.kd * (error - self._prev_error) / dt

        self._prev_time = now
        self._prev_error = error
        return p_term + self._integral + d_term


class ActuationAxis:
    """Couples one AxisPID to the "don't fight momentum" braking logic.

    Between samples the drone keeps coasting in whatever direction it was last
    commanded. If the vehicle is still moving opposite to the current setpoint,
    blindly adding this sample's PID output to the previous command would fight
    itself; instead we detect the sign flip and command a hard brake (zero +
    reset the PID) for that axis.
    """

    def __init__(self, gains: PIDGains):
        self._pid = AxisPID(gains)
        self._previous_command = 0.0

    def reset(self):
        self._pid.reset()
        self._previous_command = 0.0

    def compute(self, setpoint: float, current: float, now: float) -> int:
        error = setpoint - current
        output = self._pid.update(error, now)

        command = self._previous_command + output
        braking = _is_opposite_direction(setpoint, command)
        if braking:
            command = 0.0
            self._pid.reset()

        command = _clamp(command, PCMD_MIN, PCMD_MAX)
        self._previous_command = command
        return round(command)


def _is_opposite_direction(setpoint: float, value: float) -> bool:
    if setpoint <= 0.0 and value > 0.0:
        return True
    if setpoint >= 0.0 and value < 0.0:
        return True
    return False


class VelocityPIDThread(threading.Thread):
    """Background thread that drives PCMD to track a velocity setpoint.

    Usage: construct once, call start(), then toggle resume()/pause() as the
    driver enters/exits velocity-control flight modes and call set_target() to
    update the setpoint. The thread runs until stop() is called.
    """

    def __init__(self, drone,
                 forward_gains: PIDGains = DEFAULT_FORWARD_GAINS,
                 right_gains: PIDGains = DEFAULT_RIGHT_GAINS,
                 up_gains: PIDGains = DEFAULT_UP_GAINS):
        super().__init__(name='velocity-pid', daemon=True)
        self.drone = drone

        self._forward = ActuationAxis(forward_gains)
        self._right = ActuationAxis(right_gains)
        self._up = ActuationAxis(up_gains)

        self._lock = threading.Lock()
        self._target = common_proto.Velocity()
        self._frame = control_proto.ReferenceFrame.REFERENCE_FRAME_BODY

        self._running = threading.Event()
        self._running.set()
        self._active = threading.Event()

    def set_target(self, velocity: common_proto.Velocity, frame: control_proto.ReferenceFrame):
        """Updates the velocity setpoint the PID loop should track."""
        with self._lock:
            self._target = velocity
            self._frame = frame

    def pause(self):
        """Stops actuating PCMD until resume() is called."""
        self._active.clear()
        self._forward.reset()
        self._right.reset()
        self._up.reset()

    def resume(self):
        """Resumes actuating PCMD toward the last-set target."""
        self._active.set()

    def stop(self):
        """Permanently stops the thread; the thread cannot be restarted after this."""
        self._running.clear()
        self._active.set()  # wake the loop so it can observe _running is clear

    def run(self):
        while self._running.is_set():
            if not self._active.wait(timeout=LOOP_INTERVAL_S):
                continue
            if not self._running.is_set():
                break
            self._step()
            time.sleep(LOOP_INTERVAL_S)

    def _step(self):
        with self._lock:
            target = self._target
            frame = self._frame

        if frame == control_proto.ReferenceFrame.REFERENCE_FRAME_NEU:
            current = get_velocity_neu(self.drone)
        else:
            current = get_velocity_body(self.drone)

        now = time.monotonic()
        forward_cmd = self._forward.compute(target.x_vel, current.x_vel, now)
        right_cmd = self._right.compute(target.y_vel, current.y_vel, now)
        up_cmd = self._up.compute(target.z_vel, current.z_vel, now)

        max_rotation = self.drone.get_state(MaxRotationSpeedChanged)['max']
        angular_cmd = round(_clamp((target.angular_vel / max_rotation) * 100, PCMD_MIN, PCMD_MAX))

        if forward_cmd or right_cmd or up_cmd or angular_cmd:
            self.drone(PCMD(1, right_cmd, forward_cmd, angular_cmd, up_cmd, timestampAndSeqNum=0))
