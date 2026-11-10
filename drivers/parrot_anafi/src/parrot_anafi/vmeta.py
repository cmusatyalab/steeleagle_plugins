"""Reads the drone's video metadata (vmeta), which goes with each frame of its
video stream, and turns it into the pose a RawFrame carries.

The drone puts each frame's metadata in an RTP header extension of the
frame's first packet: https://developer.parrot.com/docs/groundsdk-tools/
video-metadata.html. The extension's profile field names the metadata's
format, and the format's own structure begins with that profile and the
extension's length, so the extension read whole is the metadata.

Parrot's libvideo-metadata, which Olympe bundles, parses it, as PDraw does.
It gives the metadata as JSON, in the form of Olympe's VideoFrame.vmeta(): the
Anafi 4K's v3 metadata is read as the protobuf form that later drones send.
"""
import ctypes
import json
import logging
import struct
from math import asin, atan2, cos, degrees, radians, sin

import olympe_deps as od

import steeleagle_protocol.v1.common.common_pb2 as common_proto
import steeleagle_protocol.v1.messages.telemetry.telemetry_pb2 as telemetry_proto

logger = logging.getLogger('parrot-anafi/vmeta')

# libvideo-metadata's MIME type of each format, by the profile of the RTP
# header extension that holds it. The Anafi 4K sends v3. Protobuf metadata
# may be split across a frame's first packets; only unsplit ones are read.
MIME_TYPES = {
    0x5032: od.VMETA_FRAME_V2_MIME_TYPE,
    0x5033: od.VMETA_FRAME_V3_MIME_TYPE,
    0x5062: od.VMETA_FRAME_PROTO_MIME_TYPE,
}
# Room for a frame's metadata as JSON; Olympe gives it as much.
JSON_SIZE = 4096
# Approximate meters per degree of latitude, used to convert GPS deltas
# into a local ENU frame for relative_position (equirectangular approximation).
METERS_PER_DEGREE_LATITUDE = 111320.0
# Latitude and longitude Olympe gives for an unknown home.
UNKNOWN_COORDINATE = 500.0


def read_rtp_extension(profile, data):
    """The metadata in an RTP header extension with the profile `profile` and
    the data `data`, as a dict, or None if it holds none, or none that can be
    read."""
    mime_type = MIME_TYPES.get(profile)
    if mime_type is None or len(data) % 4:
        return None
    raw = struct.pack('>HH', profile, len(data) // 4) + bytes(data)
    cdata = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    buf = od.struct_vmeta_buffer()
    buf.vmeta_buffer_0.cdata = ctypes.cast(cdata, od.POINTER_T(ctypes.c_ubyte))
    buf.len = len(raw)
    buf.pos = 0
    frame = od.POINTER_T(od.struct_vmeta_frame)()
    res = od.vmeta_frame_read(ctypes.byref(buf), mime_type.encode(), ctypes.byref(frame))
    if res < 0 or not frame:
        logger.debug('could not read metadata of profile %#x: error %d', profile, res)
        return None
    try:
        out = ctypes.create_string_buffer(JSON_SIZE)
        res = od.vmeta_frame_to_json_str(frame, out, ctypes.sizeof(out))
        if res < 0:
            logger.debug('could not convert metadata to JSON: error %d', res)
            return None
        return json.loads(out.value.decode())
    finally:
        od.vmeta_frame_unref(frame)


def _quaternion(q):
    """`q`, a vmeta quaternion, as (w, x, y, z), or None if it is absent or
    zero, as unset ones are."""
    if not q:
        return None
    wxyz = tuple(float(q.get(k, 0.0)) for k in 'wxyz')
    return wxyz if any(wxyz) else None


def _conjugate_product(a, b):
    """The rotation `b` relative to `a`, both unit quaternions: conj(a) * b."""
    aw, ax, ay, az = a[0], -a[1], -a[2], -a[3]
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def euler(q):
    """The (yaw, pitch, roll), in radians, of `q`, a (w, x, y, z) rotation
    from NED to a forward-right-down body, applied yaw, then pitch, then roll.
    Pitch is up, and yaw clockwise from north, as the drone reports them."""
    w, x, y, z = q
    yaw = atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    pitch = asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
    roll = atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    return yaw, pitch, roll


def _pose(q):
    yaw, pitch, roll = euler(q)
    return common_proto.Pose(pitch=degrees(pitch), roll=degrees(roll), yaw=degrees(yaw))


def valid_home(home):
    """Whether `home`, as Olympe's HomeChanged gives it, is known."""
    return (home is not None
            and home.get('latitude', UNKNOWN_COORDINATE) != UNKNOWN_COORDINATE
            and home.get('longitude', UNKNOWN_COORDINATE) != UNKNOWN_COORDINATE)


def position_info(meta, home=None, altitude=None, setpoint=None):
    """The PositionInfo of the drone in `meta`, a frame's metadata as
    read_rtp_extension gives it.

    The metadata has no home, so `home` gives it, as Olympe's HomeChanged
    does, or None if it is unknown; relative positions are north and east of
    it. Up is the altitude above take-off, which only protobuf metadata has,
    so `altitude` gives it, as Olympe's AltitudeChanged does, for the rest;
    the metadata's ground distance is a height above the ground, not the
    take-off point, so it is not used. `setpoint`, an Any, is the drone's
    guidance setpoint.
    """
    drone = meta.get('drone', {})
    location = drone.get('location')
    q = _quaternion(drone.get('quat'))
    yaw = euler(q)[0] if q is not None else None

    global_position = None
    if location is not None:
        global_position = common_proto.GlobalPosition(
            latitude=location.get('latitude'),
            longitude=location.get('longitude'),
            altitude=location.get('altitude_egm96amsl', location.get('altitude_wgs84ellipsoid')),
            heading=degrees(yaw) % 360 if yaw is not None else None,
        )
    known_home = valid_home(home)
    relative_position = common_proto.RelativePosition(
        z=drone.get('altitude_ato', altitude),
        angle=degrees(yaw) if yaw is not None else None,
    )
    if location is not None and known_home:
        relative_position.x = (location['latitude'] - home['latitude']) * METERS_PER_DEGREE_LATITUDE
        relative_position.y = ((location['longitude'] - home['longitude'])
                               * METERS_PER_DEGREE_LATITUDE * cos(radians(home['latitude'])))

    velocity_body = velocity_neu = None
    speed = drone.get('speed')
    if speed is not None:
        north, east = speed.get('north', 0.0), speed.get('east', 0.0)
        up = -speed.get('down', 0.0)
        velocity_neu = common_proto.Velocity(x_vel=north, y_vel=east, z_vel=up)
        if yaw is not None:
            velocity_body = common_proto.Velocity(
                x_vel=north * cos(yaw) + east * sin(yaw),
                y_vel=-north * sin(yaw) + east * cos(yaw),
                z_vel=up,
            )

    return telemetry_proto.PositionInfo(
        home=common_proto.GlobalPosition(
            latitude=home['latitude'],
            longitude=home['longitude'],
            altitude=home.get('altitude'),
        ) if known_home and location is not None else None,
        global_position=global_position,
        relative_position=relative_position,
        velocity_body=velocity_body,
        velocity_neu=velocity_neu,
        setpoint=setpoint,
    )


def gimbal_info(meta, setpoint=None):
    """The GimbalInfo of the camera in `meta`, a frame's metadata as
    read_rtp_extension gives it: the frame's view in NED, and relative to the
    drone's body. `setpoint`, an Any, is the gimbal's setpoint."""
    camera = _quaternion(meta.get('camera', {}).get('quat'))
    drone = _quaternion(meta.get('drone', {}).get('quat'))
    return telemetry_proto.GimbalInfo(
        pose_neu=_pose(camera) if camera is not None else None,
        pose_body=_pose(_conjugate_product(drone, camera))
        if camera is not None and drone is not None else None,
        gimbal_setpoint=setpoint,
    )
