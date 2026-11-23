"""What the vehicle and remote engines share: payload types, the reachback
engine's producers, frame helpers, and the NFN status the swiftmap engine
tells reachback."""

import json

from google.protobuf.message import DecodeError

from steeleagle_protocol.v1.messages.frame import frame_pb2

RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"
ENCODED_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/EncodedFrame"
GENERIC_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/GenericResult"
STATUS_TYPE = "swiftmap/NfnStatus"

# The reachback engine's producers, published as {engine}.{producer}: new
# keyframes, and frames sent back for a request, then an empty end marker
KEYFRAMES = "keyframes"
SENDBACK = "sendback"

# The protocol has no WebP codec yet: WebP goes as CODEC_UNSPECIFIED.
WEBP_CODEC = getattr(frame_pb2, "CODEC_WEBP", frame_pb2.CODEC_UNSPECIFIED)
POLL_INTERVAL_S = 0.1
STATS_INTERVAL_S = 5.0


class FrameError(Exception):
    pass


def same_type(payload_type, expected):
    """Whether `payload_type` is `expected`, written with slashes or dots."""
    return payload_type.replace(".", "/") == expected


def producer_name(engine, producer):
    """The name the engine `engine` publishes its producer `producer` on."""
    return f"{engine}.{producer}"


def is_sendback(input_id):
    """Whether the input came from a reachback engine's sendback producer."""
    return input_id.producer.endswith(f".{SENDBACK}")


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


def encode_status(hold, request):
    """The NFN status: whether the server holds, and the current request,
    (targets [[lat, lon, alt], ...], frames to send back) or None."""
    targets, count = request if request is not None else (None, None)
    return json.dumps({"hold": hold, "request": None if request is None
                       else {"targets": targets, "count": count}}).encode()


def decode_status(data):
    """(hold, (targets, count) or None); ValueError if `data` is not a status."""
    try:
        status = json.loads(data)
        hold, request = status["hold"], status["request"]
        if not isinstance(hold, bool):
            raise ValueError("hold is not true or false")
        if request is not None:
            request = ([list(map(float, t)) for t in request["targets"]], int(request["count"]))
    except (KeyError, TypeError, ValueError) as err:
        raise ValueError(f"not an NFN status: {err}") from None
    if request is not None and (request[1] < 0 or any(len(t) != 3 for t in request[0])):
        raise ValueError("a target is lat, lon, alt, and the count is not negative")
    return hold, request
