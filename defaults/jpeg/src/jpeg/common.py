"""Shared GStreamer plumbing for the jpeg encoder and decoder: a pipeline an
engine pushes one buffer into and pulls what it became from, matched by
timestamp.
"""
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GstApp  # noqa: E402,F401

Gst.init(None)

# RawFrame pixels are RGB, one byte per channel.
RGB_CHANNELS = 3
# Room in a frame for its fields besides its pixels: its ids, timestamp and
# pose.
FRAME_HEADROOM = 16 << 10
# The default largest output of either engine: a 1080p RGB RawFrame.
MAX_FRAME_SIZE = 1920 * 1080 * RGB_CHANNELS + FRAME_HEADROOM

# Where a pipeline's timestamps start. An encoder based on GstVideoEncoder
# shifts every timestamp by 1000 hours when a decoding timestamp would
# otherwise be negative, as it would be near 0; timestamps that start there
# are never shifted, so outputs keep their inputs' stamps.
PTS_BASE = 1000 * 60 * 60 * Gst.SECOND


def _rgb_stride(width):
    """GStreamer pads each row of an RGB frame to a multiple of 4 bytes."""
    return (width * RGB_CHANNELS + 3) & ~3


def pad_rows(data, width, height):
    """Tightly packed RGB pixels as GStreamer lays them out."""
    row, stride = width * RGB_CHANNELS, _rgb_stride(width)
    if row == stride:
        return data
    pad = bytes(stride - row)
    return b"".join(data[y * row : (y + 1) * row] + pad for y in range(height))


def unpad_rows(data, width, height):
    """RGB pixels as GStreamer lays them out, tightly packed."""
    row, stride = width * RGB_CHANNELS, _rgb_stride(width)
    if row == stride:
        return data[: row * height]
    return b"".join(data[y * stride : y * stride + row] for y in range(height))


def _bus_error(pipeline):
    bus = pipeline.get_bus()
    if bus is None:
        return "no bus"
    msg = bus.pop_filtered(Gst.MessageType.ERROR)
    if msg is None:
        return "no error message"
    err, _debug = msg.parse_error()
    return str(err)


class Pipeline:
    """A pipeline, in gst-launch syntax, from an appsrc named 'src' to an
    appsink named 'sink'."""

    def __init__(self, description, src_caps):
        self.pipeline = Gst.parse_launch(description)
        self.src = self.pipeline.get_by_name("src")
        self.sink = self.pipeline.get_by_name("sink")
        if self.src is None:
            raise RuntimeError("the pipeline has no element named src")
        if self.sink is None:
            raise RuntimeError("the pipeline has no element named sink")

        self.src.set_property("caps", Gst.Caps.from_string(src_caps))
        self.src.set_property("format", Gst.Format.TIME)
        self.src.set_property("is-live", True)
        self.sink.set_property("sync", False)

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError(f"failed to start the pipeline: {_bus_error(self.pipeline)}")

    def push(self, data, pts_ns):
        """Pushes the bytes `data`, stamped `pts_ns` (int nanoseconds),
        which must differ from the stamps of other buffers."""
        buf = Gst.Buffer.new_wrapped(data)
        buf.pts = PTS_BASE + pts_ns
        ret = self.src.push_buffer(buf)
        if ret != Gst.FlowReturn.OK:
            raise RuntimeError(f"failed to push a buffer ({ret}): {_bus_error(self.pipeline)}")

    def pull(self, pts_ns, timeout_ms):
        """The output of the buffer pushed with `pts_ns`, if it comes within
        `timeout_ms`. Outputs of earlier buffers, which came too late, are
        dropped."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            sample = self.sink.try_pull_sample(int(left * Gst.SECOND))
            if sample is None:
                return None
            buf = sample.get_buffer()
            if buf is not None and buf.pts == PTS_BASE + pts_ns:
                return sample

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)
