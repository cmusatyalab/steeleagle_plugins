"""What every codec of the thumbnail plugins shares: the payload types it
reads and writes, the size of an RGB RawFrame, and the fields a frame keeps
when it is encoded or decoded."""

# Uriel payload types, as named in plugin configs.
RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"
ENCODED_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/EncodedFrame"

# RawFrame pixels are RGB, one byte per channel.
RGB_CHANNELS = 3
# Room in a frame for its fields besides its pixels: its ids, timestamp and
# pose.
FRAME_HEADROOM = 16 << 10
# The default largest output of every engine: a 1080p RGB RawFrame.
MAX_FRAME_SIZE = 1920 * 1080 * RGB_CHANNELS + FRAME_HEADROOM

# Fields copied from a frame to what it is encoded or decoded to.
_FRAME_FIELDS = ("timestamp", "gimbal_info", "position_info")


def copy_fields(src, dst):
    """Copies the timestamp and pose of the frame `src` to `dst`."""
    for field in _FRAME_FIELDS:
        if src.HasField(field):
            getattr(dst, field).CopyFrom(getattr(src, field))


def same_type(payload_type, expected):
    """Whether `payload_type` is `expected`, written with slashes or dots."""
    return payload_type.replace(".", "/") == expected
