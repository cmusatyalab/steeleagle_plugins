"""What the vehicle and remote engines share: payload types, frame helpers,
and the NFN messages between reachback and swiftmap."""

import json
import struct
from typing import NamedTuple

from google.protobuf.message import DecodeError

from steeleagle_protocol.v1.messages.frame import frame_pb2

RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"
ENCODED_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/EncodedFrame"
GENERIC_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/GenericResult"
REACHBACK_FRAME_TYPE = "swiftmap/ReachbackFrame"
REACHBACK_DONE_TYPE = "swiftmap/ReachbackDone"
STATUS_TYPE = "swiftmap/NfnStatus"

# The protocol has no WebP codec yet: WebP goes as CODEC_UNSPECIFIED.
WEBP_CODEC = getattr(frame_pb2, "CODEC_WEBP", frame_pb2.CODEC_UNSPECIFIED)
POLL_INTERVAL_S = 0.1
STATS_INTERVAL_S = 5.0
# Request id and frame number (or, on the end, frame count) before a payload.
STAMP_FMT = "!QI"
STAMP_NBYTES = struct.calcsize(STAMP_FMT)


class FrameError(Exception):
    pass


def same_type(payload_type, expected):
    """Whether `payload_type` is `expected`, written with slashes or dots."""
    return payload_type.replace(".", "/") == expected


def read_frame(payload_type, data):
    """The EncodedFrame `data`."""
    if not same_type(payload_type, ENCODED_FRAME_TYPE):
        raise FrameError(f"payload is a {payload_type!r}, not an EncodedFrame")
    try:
        return frame_pb2.EncodedFrame.FromString(data)
    except DecodeError as err:
        raise FrameError(f"bad {payload_type}: {err}") from None


def is_webp(data):
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def webp_bytes(frame):
    """The EncodedFrame's WebP file, as the preprocess engine sends it."""
    if frame.codec in (WEBP_CODEC, frame_pb2.CODEC_UNSPECIFIED) and is_webp(frame.encoded_data):
        return frame.encoded_data
    raise FrameError(f"{frame_pb2.Codec.Name(frame.codec)} frames are not supported, only WebP "
                     "from the preprocess engine")


def frame_gps(frame):
    """(lat, lon, alt) of the frame, or None without a fix; altitude defaults to 0."""
    if not frame.HasField("position_info") or not frame.position_info.HasField("global_position"):
        return None
    g = frame.position_info.global_position
    if not (g.HasField("latitude") and g.HasField("longitude")):
        return None
    return (g.latitude, g.longitude, g.altitude if g.HasField("altitude") else 0.0)


def stamp(request_id, number, data=b""):
    return struct.pack(STAMP_FMT, request_id, number) + data


def unstamp(data):
    """(request id, number, data) of a stamped payload."""
    if len(data) < STAMP_NBYTES:
        raise FrameError(f"{len(data)} bytes is too short to be stamped")
    request_id, number = struct.unpack_from(STAMP_FMT, data)
    return request_id, number, data[STAMP_NBYTES:]


class Request(NamedTuple):
    """A plan's request for `count` frames covering `targets` ([lat, lon, alt]).
    Ids are never reused, across restarts too."""

    id: int
    targets: list
    count: int


def encode_status(hold, request):
    """The NFN status: whether the server holds, and the current Request or None."""
    return json.dumps({"hold": hold, "request": None if request is None else request._asdict()}).encode()


def decode_status(data):
    """(hold, Request or None); ValueError if `data` is not a status."""
    try:
        status = json.loads(data)
        hold, request = status["hold"], status["request"]
        if not isinstance(hold, bool):
            raise ValueError("hold is not true or false")
        if request is not None:
            request = Request(int(request["id"]), [list(map(float, t)) for t in request["targets"]],
                              int(request["count"]))
    except (KeyError, TypeError, ValueError) as err:
        raise ValueError(f"not an NFN status: {err}") from None
    if request is not None and (request.id < 0 or request.count < 0
                                or any(len(t) != 3 for t in request.targets)):
        raise ValueError("a target is lat, lon, alt, and the id and count are not negative")
    return hold, request
