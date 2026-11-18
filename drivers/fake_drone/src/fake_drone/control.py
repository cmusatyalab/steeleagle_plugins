"""A stand-in ControlService: the fake drone accepts every command and
reports it done, without moving, since it has no vehicle to move.

It is there so the drone has a copter's commands: the mission compiler (the
GCS's inferred mission, eaglec) generates the SDK's Vehicle from the driver's
supported services, and without ControlService no mission compiles. A
mission that runs on it does nothing but log its commands.
"""
import logging

import steeleagle_protocol.v1.messages.telemetry.telemetry_pb2 as telemetry_proto
import steeleagle_protocol.v1.services.driver.control_pb2 as control_proto
from steeleagle_protocol.v1.services.driver.control_pb2_grpc import ControlServiceServicer

logger = logging.getLogger('fake-drone/control')

Mode = telemetry_proto.Mode
Status = telemetry_proto.MotionStatus


def _command(name, mode=None, status=None):
    """The RPC method `name`: logs the request and returns its response, with
    the mode and motion status a real copter would report for it, if any."""
    response = getattr(control_proto, f'{name}Response')

    def method(self, request, context):
        logger.info('%s (ignored: nothing to move): %s', name, str(request).replace('\n', ' ').strip())
        fields = response.DESCRIPTOR.fields_by_name
        kwargs = {}
        if mode is not None and 'expected_mode' in fields:
            kwargs['expected_mode'] = mode
        if status is not None and 'expected_status' in fields:
            kwargs['expected_status'] = status
        return response(**kwargs)

    method.__name__ = name
    return method


class Control(ControlServiceServicer):
    """ControlService for the fake drone; Stand and Sit, which copters don't
    have, are left unimplemented (and listed unsupported in the plugin TOML)."""

    TakeOff = _command('TakeOff', Mode.MODE_TAKEOFF, Status.MOTION_STATUS_HOLDING)
    Land = _command('Land', Mode.MODE_LAND, Status.MOTION_STATUS_STOPPED)
    Hold = _command('Hold', Mode.MODE_LOITER, Status.MOTION_STATUS_HOLDING)
    Kill = _command('Kill', Mode.MODE_EMERGENCY, Status.MOTION_STATUS_STOPPED)
    ReturnToHome = _command('ReturnToHome', Mode.MODE_RETURN_TO_HOME, Status.MOTION_STATUS_HOLDING)
    SetGlobalPositionTarget = _command('SetGlobalPositionTarget')
    SetRelativePositionTarget = _command('SetRelativePositionTarget')
    SetVelocityTarget = _command('SetVelocityTarget')
    SetGimbalAngleTarget = _command('SetGimbalAngleTarget')
    SetGimbalVelocityTarget = _command('SetGimbalVelocityTarget')
