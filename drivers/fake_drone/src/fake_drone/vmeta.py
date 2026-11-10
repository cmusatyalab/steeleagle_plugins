"""Reads the frame telemetry a Parrot drone records into its MP4 files, and
turns it into the PositionInfo and GimbalInfo that go with each RawFrame.

The format is Parrot's video metadata, described at
https://developer.parrot.com/docs/groundsdk-tools/video-metadata.html: one
sample per video frame, in a timed metadata track (an `mett` sample entry
whose MIME type names the version), which references the video track. The
ANAFI range records the v3 binary format; v2 (Bebop, Disco), whose base
holds the same telemetry, is read too. Later drones (ANAFI Ai, ANAFI UKR)
record protobuf, which isn't read here.

All fields are big-endian fixed point; see the annex of the page above.
"""
import bisect
import logging
import math
import struct
from dataclasses import dataclass

import steeleagle_protocol.v1.common.common_pb2 as common_proto
import steeleagle_protocol.v1.messages.telemetry.telemetry_pb2 as telemetry_proto

from fake_drone import mp4

logger = logging.getLogger('fake-drone/vmeta')

# MIME type of the metadata track of each format read here, by base id
MIME_PREFIX = 'application/octet-stream;type='
FORMATS = {
    'com.parrot.videometadata2': 0x5032,  # 'P2'
    'com.parrot.videometadata3': 0x5033,  # 'P3'
}
# Base structures, after the id and length fields
_V2_BASE = struct.Struct('>iiii4h4h4h2hHHBBbB')
_V3_BASE = struct.Struct('>iiii4h4h4h4hHHHHHHIbBBB')
_HEADER = struct.Struct('>HH')
# Extension holding the frame's capture time ('E1'), in µs of the drone's
# monotonic clock
TIMESTAMP_EXT = 0x4531
# Latitude and longitude of a location the drone doesn't know
UNKNOWN_LOCATION = 500.0
# Approximate meters per degree of latitude, as parrot_anafi converts GPS
# deltas into a local frame (equirectangular approximation)
METERS_PER_DEGREE_LATITUDE = 111320.0

Q2_14 = 1 << 14
Q8_8 = 1 << 8
Q10_22 = 1 << 22
Q16_16 = 1 << 16


@dataclass(frozen=True)
class Quaternion:
    w: float
    x: float
    y: float
    z: float

    @classmethod
    def from_q2_14(cls, w, x, y, z):
        return cls(w / Q2_14, x / Q2_14, y / Q2_14, z / Q2_14)

    def conjugate(self):
        return Quaternion(self.w, -self.x, -self.y, -self.z)

    def __mul__(self, o):
        return Quaternion(
            self.w * o.w - self.x * o.x - self.y * o.y - self.z * o.z,
            self.w * o.x + self.x * o.w + self.y * o.z - self.z * o.y,
            self.w * o.y - self.x * o.z + self.y * o.w + self.z * o.x,
            self.w * o.z + self.x * o.y - self.y * o.x + self.z * o.w,
        )

    def euler(self):
        """(yaw, pitch, roll) in degrees, ZYX as libvideo-metadata's
        vmeta_quat_to_euler gives them."""
        w, x, y, z = self.w, self.x, self.y, self.z
        roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
        pitch = math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
        yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
        return math.degrees(yaw), math.degrees(pitch), math.degrees(roll)


@dataclass(frozen=True)
class FrameMetadata:
    """The telemetry of one frame, in SI units and degrees."""
    # Best estimate of the height above the ground (m)
    ground_distance: float
    # None if the drone had no GPS fix
    latitude: float | None
    longitude: float | None
    # Above the EGM96 geoid (m)
    altitude: float
    satellites: int
    north_speed: float
    east_speed: float
    down_speed: float
    drone: Quaternion
    # Orientation of the camera's view, in NED
    frame: Quaternion
    battery_percentage: int
    flying_state: int
    # µs of the drone's monotonic clock, or None
    capture_time: int | None = None


def parse(data):
    """The FrameMetadata in `data`, a v2 or v3 sample, or None if it is
    neither or is truncated."""
    if len(data) < _HEADER.size:
        return None
    ident, length = _HEADER.unpack_from(data)
    end = min(len(data), _HEADER.size + 4 * length)
    if ident == FORMATS['com.parrot.videometadata3'] and end >= _HEADER.size + _V3_BASE.size:
        (ground, lat, lon, alt_sv, vn, ve, vd, _air,
         dw, dx, dy, dz, _bw, _bx, _by, _bz, fw, fx, fy, fz,
         _exposure, _gain, _awb_r, _awb_b, _hfov, _vfov, _link,
         _rssi, battery, state, _mode) = _V3_BASE.unpack_from(data, _HEADER.size)
        base_end = _HEADER.size + _V3_BASE.size
    elif ident == FORMATS['com.parrot.videometadata2'] and end >= _HEADER.size + _V2_BASE.size:
        (ground, lat, lon, alt_sv, vn, ve, vd, _air,
         dw, dx, dy, dz, fw, fx, fy, fz, _pan, _tilt,
         _exposure, _gain, state, _mode, _rssi, battery) = _V2_BASE.unpack_from(data, _HEADER.size)
        state &= 0x7F  # bit 7 is binning
        base_end = _HEADER.size + _V2_BASE.size
    else:
        return None
    latitude, longitude = lat / Q10_22, lon / Q10_22
    if latitude == UNKNOWN_LOCATION or longitude == UNKNOWN_LOCATION:
        latitude = longitude = None
    return FrameMetadata(
        ground_distance=ground / Q16_16,
        latitude=latitude,
        longitude=longitude,
        # Bits 31..8 are the altitude, Q16.8; >> on the signed int keeps its sign
        altitude=(alt_sv >> 8) / Q8_8,
        satellites=alt_sv & 0xFF,
        north_speed=vn / Q8_8,
        east_speed=ve / Q8_8,
        down_speed=vd / Q8_8,
        drone=Quaternion.from_q2_14(dw, dx, dy, dz),
        frame=Quaternion.from_q2_14(fw, fx, fy, fz),
        battery_percentage=battery,
        flying_state=state,
        capture_time=_capture_time(data, base_end, end),
    )


def _capture_time(data, pos, end):
    """The timestamp extension's time, among the extensions from `pos`."""
    while pos + _HEADER.size <= end:
        ext_id, ext_length = _HEADER.unpack_from(data, pos)
        body = pos + _HEADER.size
        if ext_id == TIMESTAMP_EXT and ext_length >= 2 and body + 8 <= end:
            high, low = struct.unpack_from('>II', data, body)
            return high << 32 | low
        pos = body + 4 * ext_length
    return None


class Telemetry:
    """The recorded telemetry of an MP4 file, looked up by a video frame's
    presentation time. The first GPS fix in the recording stands in for the
    drone's home, from which relative positions are measured."""

    def __init__(self, times, frames, tolerance):
        self._times = times
        self._frames = frames
        self._tolerance = tolerance
        self.home = next((m for m in frames if m.latitude is not None), None)

    def __len__(self):
        return len(self._frames)

    @classmethod
    def read(cls, path):
        """The telemetry recorded in the MP4 file at `path`. Raises ValueError
        if it has none in a format read here."""
        movie = mp4.read_movie(path)
        track = next((t for t in movie.tracks
                      if t.codec == 'mett' and t.mime.startswith(MIME_PREFIX)
                      and t.mime[len(MIME_PREFIX):] in FORMATS), None)
        if track is None:
            mimes = [t.mime for t in movie.tracks if t.codec == 'mett']
            raise ValueError(f'{path} has no Parrot v2/v3 frame metadata track '
                             f'(metadata tracks: {mimes or "none"})')
        video = next((t for t in movie.tracks if t.track_id in track.describes), None)
        if video is None:
            video = next((t for t in movie.tracks if t.handler == 'vide'), None)
        # A frame's metadata is the latest at or before it, up to half a frame
        # late, so that rounding of either track's times doesn't skip it
        tolerance = 0.0
        if video is not None and video.sample_duration:
            tolerance = video.sample_duration / video.timescale / 2
        times, frames = [], []
        for t, data in zip(mp4.sample_times(movie, track), mp4.read_samples(path, track)):
            meta = parse(data)
            if meta is not None:
                times.append(t)
                frames.append(meta)
        if not frames:
            raise ValueError(f'{path} has a {track.mime} track, but none of its '
                             f'{len(track.sizes)} samples could be read')
        logger.info('read %d frames of %s telemetry from %s', len(frames),
                    track.mime[len(MIME_PREFIX):], path)
        return cls(times, frames, tolerance)

    def at(self, seconds):
        """The FrameMetadata of the frame presented at `seconds`, or None if
        the recording has none that early."""
        i = bisect.bisect_right(self._times, seconds + self._tolerance) - 1
        return self._frames[i] if i >= 0 else None

    def pose(self, seconds):
        """(PositionInfo, GimbalInfo) of the frame presented at `seconds`;
        both are empty if it has no telemetry."""
        meta = self.at(seconds)
        if meta is None:
            return telemetry_proto.PositionInfo(), telemetry_proto.GimbalInfo()
        return self.position_info(meta), gimbal_info(meta)

    def position_info(self, meta):
        yaw, _, _ = meta.drone.euler()
        psi = math.radians(yaw)
        vn, ve, vu = meta.north_speed, meta.east_speed, -meta.down_speed
        info = telemetry_proto.PositionInfo(
            relative_position=common_proto.RelativePosition(
                z=meta.ground_distance, angle=yaw,
            ),
            velocity_body=common_proto.Velocity(
                x_vel=vn * math.cos(psi) + ve * math.sin(psi),
                y_vel=-vn * math.sin(psi) + ve * math.cos(psi),
                z_vel=vu,
            ),
            velocity_neu=common_proto.Velocity(x_vel=vn, y_vel=ve, z_vel=vu),
        )
        home = self.home
        if meta.latitude is not None:
            info.global_position.CopyFrom(common_proto.GlobalPosition(
                latitude=meta.latitude,
                longitude=meta.longitude,
                altitude=meta.altitude,
                heading=yaw % 360,
            ))
            info.home.CopyFrom(common_proto.GlobalPosition(
                latitude=home.latitude,
                longitude=home.longitude,
                altitude=home.altitude,
            ))
            info.relative_position.x = (meta.latitude - home.latitude) * METERS_PER_DEGREE_LATITUDE
            info.relative_position.y = ((meta.longitude - home.longitude) * METERS_PER_DEGREE_LATITUDE
                                        * math.cos(math.radians(home.latitude)))
        else:
            info.relative_position.x = 0.0
            info.relative_position.y = 0.0
        return info


def gimbal_info(meta):
    """The camera's pose in NED (pose_neu) and relative to the drone's body
    (pose_body), from the frame view and drone quaternions."""
    yaw, pitch, roll = meta.frame.euler()
    body_yaw, body_pitch, body_roll = (meta.drone.conjugate() * meta.frame).euler()
    return telemetry_proto.GimbalInfo(
        pose_body=common_proto.Pose(pitch=body_pitch, roll=body_roll, yaw=body_yaw),
        pose_neu=common_proto.Pose(pitch=pitch, roll=roll, yaw=yaw),
    )
