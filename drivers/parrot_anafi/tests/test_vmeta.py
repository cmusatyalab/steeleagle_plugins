"""Tests of reading frame metadata (vmeta) from the drone's RTP packets, and
of the pose it gives a RawFrame.

These need no drone: metadata is written by libvideo-metadata, as the drone
writes it, into RTP packets made here.
"""
import ctypes
import struct
from math import cos, radians, sin

import olympe_deps as od
import pytest
from google.protobuf.any_pb2 import Any

import steeleagle_protocol.v1.common.common_pb2 as common_proto

from parrot_anafi import capture, vmeta
from parrot_anafi.capture import GstCapture, Gst

LATITUDE, LONGITUDE, ALTITUDE = 40.4433, -79.9436, 300.5
HOME = {'latitude': 40.4430, 'longitude': -79.9440, 'altitude': 290.0}
GROUND_DISTANCE = 12.25
# Altitude above take-off, as Olympe's AltitudeChanged gives it
TAKEOFF_ALTITUDE = 8.5
NORTH, EAST, DOWN = 1.5, -2.0, 0.5
# Drone heading 90 degrees (east); camera heading 90 degrees, pitched down 30
DRONE_YAW, CAMERA_PITCH = 90.0, -30.0
# A frame interval of the drone's 29.97 fps stream
FRAME_NS = 33_366_700


def quaternion(yaw=0.0, pitch=0.0, roll=0.0):
    """(w, x, y, z) of the rotation by `yaw`, then `pitch`, then `roll`, in
    degrees."""
    cy, sy = cos(radians(yaw) / 2), sin(radians(yaw) / 2)
    cp, sp = cos(radians(pitch) / 2), sin(radians(pitch) / 2)
    cr, sr = cos(radians(roll) / 2), sin(radians(roll) / 2)
    return (cr * cp * cy + sr * sp * sy, sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy, cr * cp * sy - sr * sp * cy)


def set_quaternion(q, wxyz):
    q.w, q.x, q.y, q.z = wxyz


def v3_metadata(location=True):
    """Metadata in the Anafi 4K's v3 format, as libvideo-metadata writes it:
    a 0x5033 profile, a length in words, and the structure."""
    frame = od.POINTER_T(od.struct_vmeta_frame)()
    assert od.vmeta_frame_new(od.VMETA_FRAME_TYPE_V3, ctypes.byref(frame)) == 0
    try:
        base = frame.contents.vmeta_frame_0.v3.base
        set_quaternion(base.drone_quat, quaternion(yaw=DRONE_YAW))
        set_quaternion(base.frame_quat, quaternion(yaw=DRONE_YAW, pitch=CAMERA_PITCH))
        if location:
            base.location.latitude = LATITUDE
            base.location.longitude = LONGITUDE
            base.location.altitude_egm96amsl = ALTITUDE
            base.location.sv_count = 12
            base.location.valid = 1
        base.ground_distance = GROUND_DISTANCE
        base.speed.north, base.speed.east, base.speed.down = NORTH, EAST, DOWN
        out = (ctypes.c_ubyte * 1024)()
        buf = od.struct_vmeta_buffer()
        buf.vmeta_buffer_0.data = ctypes.cast(out, od.POINTER_T(ctypes.c_ubyte))
        buf.len = len(out)
        assert od.vmeta_frame_write(ctypes.byref(buf), frame) == 0
        return bytes(out[:buf.pos])
    finally:
        od.vmeta_frame_unref(frame)


def rtp_packet(pts, extension=None, sequence=0):
    """An RTP packet of H.264, timestamped `pts`, with the header extension
    `extension`, its profile and length included, if given."""
    first = 0x90 if extension is not None else 0x80
    header = struct.pack('>BBHII', first, 96, sequence, pts // 11111, 0x1234)
    packet = Gst.Buffer.new_wrapped(header + (extension or b'') + b'\x65\x88\x84')
    packet.pts = pts
    return packet


@pytest.fixture
def cap():
    """A capture that reads no stream, to be given packets by hand: its
    pipeline fails to build."""
    c = GstCapture('no-such-element', 'no-such-decoder')
    assert not c.isOpened()
    return c


def test_reads_v3_metadata():
    data = v3_metadata()
    profile, words = struct.unpack('>HH', data[:4])
    assert profile == 0x5033 and words * 4 == len(data) - 4

    meta = vmeta.read_rtp_extension(profile, data[4:])

    drone = meta['drone']
    assert drone['location']['latitude'] == pytest.approx(LATITUDE, abs=1e-6)
    assert drone['location']['sv_count'] == 12
    assert drone['ground_distance'] == pytest.approx(GROUND_DISTANCE)
    assert drone['speed']['east'] == pytest.approx(EAST)


def test_unreadable_extensions_have_no_metadata():
    data = v3_metadata()[4:]
    assert vmeta.read_rtp_extension(0xbede, data) is None  # one-byte header extensions
    assert vmeta.read_rtp_extension(0x5033, data[:-1]) is None


def test_a_frame_gets_the_metadata_of_its_packets(cap):
    cap.add_packet(rtp_packet(0, v3_metadata()))
    cap.add_packet(rtp_packet(1_000_000, sequence=1))  # a later packet of the same frame
    cap.add_packet(rtp_packet(FRAME_NS, v3_metadata(location=False), sequence=2))

    first = cap._take_metadata(1_000_000)
    second = cap._take_metadata(FRAME_NS)

    assert 'location' in first['drone']
    assert 'location' not in second['drone']


def test_skipped_frames_drop_their_metadata(cap):
    cap.add_packet(rtp_packet(0, v3_metadata()))
    cap.add_packet(rtp_packet(FRAME_NS, v3_metadata(location=False), sequence=1))

    # The first frame was dropped before it was read
    meta = cap._take_metadata(FRAME_NS)

    assert 'location' not in meta['drone']
    assert not cap._pending_metadata


def test_a_frame_without_its_own_metadata_gets_none(cap):
    cap.add_packet(rtp_packet(0, v3_metadata()))
    cap.add_packet(rtp_packet(FRAME_NS, sequence=1))  # sent without metadata

    assert cap._take_metadata(FRAME_NS) is None
    assert cap._take_metadata(Gst.CLOCK_TIME_NONE) is None


def test_pending_metadata_is_bounded(cap):
    for i in range(capture.MAX_PENDING_METADATA + 5):
        cap.add_packet(rtp_packet(i * FRAME_NS, v3_metadata(), sequence=i))
    assert len(cap._pending_metadata) == capture.MAX_PENDING_METADATA


def test_the_rtsp_source_names_its_depayloader():
    assert f'name={capture.DEPAYLOADER_NAME}' in capture.rtsp_source('192.0.2.1')


def read(location=True):
    data = v3_metadata(location)
    return vmeta.read_rtp_extension(0x5033, data[4:])


def test_position_info_of_v3_metadata():
    setpoint = Any()
    setpoint.Pack(common_proto.Velocity(x_vel=1.0))

    position = vmeta.position_info(read(), HOME, TAKEOFF_ALTITUDE, setpoint)

    gp = position.global_position
    assert (gp.latitude, gp.longitude) == pytest.approx((LATITUDE, LONGITUDE), abs=1e-6)
    assert gp.altitude == pytest.approx(ALTITUDE)
    assert gp.heading == pytest.approx(DRONE_YAW, abs=0.1)
    assert position.home.latitude == HOME['latitude']
    rel = position.relative_position
    assert rel.x == pytest.approx((LATITUDE - HOME['latitude']) * vmeta.METERS_PER_DEGREE_LATITUDE, rel=1e-3)
    assert rel.y > 0  # east of home
    assert rel.z == TAKEOFF_ALTITUDE  # not the ground distance
    assert rel.angle == pytest.approx(DRONE_YAW, abs=0.1)
    neu = position.velocity_neu
    assert (neu.x_vel, neu.y_vel, neu.z_vel) == pytest.approx((NORTH, EAST, -DOWN), abs=0.01)
    # Heading east, north is to the left and east ahead
    body = position.velocity_body
    assert (body.x_vel, body.y_vel, body.z_vel) == pytest.approx((EAST, -NORTH, -DOWN), abs=0.01)
    assert position.setpoint == setpoint


def test_position_info_without_a_gps_fix():
    position = vmeta.position_info(read(location=False), HOME, TAKEOFF_ALTITUDE)

    assert not position.HasField('global_position')
    assert not position.HasField('home')
    rel = position.relative_position
    assert not rel.HasField('x') and not rel.HasField('y')
    assert rel.z == TAKEOFF_ALTITUDE


def test_position_info_with_an_unknown_home():
    unknown = {'latitude': 500.0, 'longitude': 500.0, 'altitude': 500.0}
    position = vmeta.position_info(read(), unknown)

    assert position.HasField('global_position')
    assert not position.HasField('home')
    assert not position.relative_position.HasField('x')


def test_an_unknown_altitude_is_unset():
    assert not vmeta.position_info(read(), HOME).relative_position.HasField('z')


def test_gimbal_info_of_v3_metadata():
    gimbal = vmeta.gimbal_info(read())

    neu, body = gimbal.pose_neu, gimbal.pose_body
    assert (neu.yaw, neu.pitch, neu.roll) == pytest.approx((DRONE_YAW, CAMERA_PITCH, 0.0), abs=0.2)
    assert (body.yaw, body.pitch, body.roll) == pytest.approx((0.0, CAMERA_PITCH, 0.0), abs=0.2)


def test_euler_inverts_quaternions():
    for angles in [(0, 0, 0), (45, -30, 10), (-120, 60, -5), (179, -89, 0)]:
        yaw, pitch, roll = vmeta.euler(quaternion(*angles))
        assert tuple(map(radians, angles)) == pytest.approx((yaw, pitch, roll), abs=1e-6)
