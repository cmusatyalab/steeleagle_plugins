"""Rescaling of RawFrames, as the Anafi driver rescaled the frames it
published: with OpenCV's area interpolation, and only if a frame is not
already at the target resolution."""

import cv2
import numpy as np

from steeleagle_protocol.v1.messages.frame import frame_pb2

RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"
# Engines read a RawFrame's pixels as RGB, one byte per channel.
RGB_CHANNELS = 3
# Room in an output for a RawFrame's fields besides its pixels: its ids,
# timestamp and pose.
RAW_FRAME_HEADROOM = 16 << 10

# Height of each value of the resolution arg; the aspect ratio sets the width
RESOLUTIONS = {
    "4k": 2160,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
    "240p": 240,
    "180p": 180,
}
DEFAULT_ASPECT_RATIO = "16:9"


def parse_aspect_ratio(aspect_ratio):
    """The aspect ratio "W:H", e.g. "16:9", as (W, H). Raises ValueError for
    anything else."""
    if isinstance(aspect_ratio, str):
        parts = aspect_ratio.strip().split(":")
        if len(parts) == 2 and all(p.strip().isdigit() for p in parts):
            w, h = (int(p) for p in parts)
            if w > 0 and h > 0:
                return w, h
    raise ValueError(f"aspect_ratio must be W:H, e.g. 16:9 or 4:3, not {aspect_ratio!r}")


def dimensions(resolution, aspect_ratio=DEFAULT_ASPECT_RATIO):
    """The dimensions (width, height) of the resolution `resolution`, e.g.
    "480p", at the aspect ratio `aspect_ratio`, e.g. "16:9": its height, and
    the width that gives the ratio, rounded to an even number of pixels.
    Raises ValueError for an unknown resolution or a bad aspect ratio."""
    key = resolution.strip().lower() if isinstance(resolution, str) else None
    if key not in RESOLUTIONS:
        raise ValueError(f"resolution must be one of {', '.join(RESOLUTIONS)}, not {resolution!r}")
    w, h = parse_aspect_ratio(aspect_ratio)
    height = RESOLUTIONS[key]
    width = max(2, 2 * round(height * w / h / 2))
    return width, height


def max_output_size(dimensions):
    """The largest RawFrame rescaled to `dimensions`: RGB pixels, and room for
    its other fields."""
    width, height = dimensions
    return width * height * RGB_CHANNELS + RAW_FRAME_HEADROOM


def rescale(data, dimensions):
    """The serialized RawFrame `data`, rescaled to `dimensions` (width,
    height), with its other fields as they were; or None if it is already at
    them. Raises ValueError if its pixels don't fit its resolution."""
    frame = frame_pb2.RawFrame.FromString(data)
    if (frame.h_res, frame.v_res) == tuple(dimensions):
        return None
    channels = frame.channels or RGB_CHANNELS
    expected = frame.h_res * frame.v_res * channels
    if expected == 0 or len(frame.data) != expected:
        raise ValueError(
            f"frame {frame.id} has {len(frame.data)} bytes of pixels, "
            f"not the {expected} of {frame.h_res}x{frame.v_res}x{channels}"
        )
    pixels = np.frombuffer(frame.data, dtype=np.uint8)
    shape = (frame.v_res, frame.h_res, channels) if channels > 1 else (frame.v_res, frame.h_res)
    resized = cv2.resize(pixels.reshape(shape), tuple(dimensions), interpolation=cv2.INTER_AREA)
    frame.data = resized.tobytes()
    frame.h_res, frame.v_res = resized.shape[1], resized.shape[0]
    frame.channels = channels
    return frame.SerializeToString()
