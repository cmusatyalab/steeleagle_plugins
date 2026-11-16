"""Shared GStreamer plumbing for the h264 encoder and decoder: a pipeline an
engine pushes one buffer into and pulls what it became from, matched by
timestamp. Mirrors gabriel-h264's Rust lib.rs.
"""
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstApp, GstVideo  # noqa: E402

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
IFRAME_TRIGGER_SIGNAL = "trigger-iframe"
# Pipeline._pool before the appsrc's peer has been asked for a pool.
_UNQUERIED = object()


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
    appsink named 'sink'. If uses_iframe_action, the encoder element must be
    named 'enc' and is force-keyframed via its trigger-iframe action
    (qtic2venc); otherwise force_keyframe sends a downstream force-key-unit
    event instead (x264enc and other GstVideoEncoder-derived encoders)."""

    def __init__(self, description, src_caps, uses_iframe_action=False):
        self.pipeline = Gst.parse_launch(description)
        self.src = self.pipeline.get_by_name("src")
        self.sink = self.pipeline.get_by_name("sink")
        if self.src is None:
            raise RuntimeError("the pipeline has no element named src")
        if self.sink is None:
            raise RuntimeError("the pipeline has no element named sink")

        self.uses_iframe_action = uses_iframe_action
        self.enc = self.pipeline.get_by_name("enc") if uses_iframe_action else None
        if uses_iframe_action and self.enc is None:
            raise RuntimeError("uses_iframe_action=True but no element named enc")

        self.src.set_property("caps", Gst.Caps.from_string(src_caps))
        self.src.set_property("format", Gst.Format.TIME)
        self.src.set_property("is-live", True)
        self.sink.set_property("sync", False)
        self.started = False
        self._pool = _UNQUERIED

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError(f"failed to start the pipeline: {_bus_error(self.pipeline)}")

    def push(self, data, pts_ns):
        """Pushes the bytes `data`, stamped `pts_ns` (int nanoseconds),
        which must differ from the stamps of other buffers."""
        self._push_buffer(Gst.Buffer.new_wrapped(data), pts_ns)

    def push_frame(self, data, width, height, pts_ns):
        """Pushes the tightly packed RGB pixels `data` of a `width`x`height`
        frame, stamped as in push.

        Hardware elements after the appsrc, such as qtivtransform, read only
        their own (GBM/DMA-buf) memory: given wrapped memory, qtivtransform
        silently outputs all-zero frames, which encode as solid green. So when
        the element after the appsrc offers a pool of such buffers, the
        pixels are copied into one of them."""
        if self._pool is _UNQUERIED:
            self._pool = self._hardware_pool()
        if self._pool is None:
            self.push(pad_rows(data, width, height), pts_ns)
            return

        ret, buf = self._pool.acquire_buffer(None)
        if ret != Gst.FlowReturn.OK:
            raise RuntimeError(f"failed to get a buffer from {self._pool.get_name()} ({ret})")
        row = width * RGB_CHANNELS
        meta = GstVideo.buffer_get_video_meta(buf)
        offset, stride = (meta.offset[0], meta.stride[0]) if meta else (0, _rgb_stride(width))
        if offset == 0 and stride == row:
            buf.fill(0, data)
        else:
            for y in range(height):
                buf.fill(offset + y * stride, data[y * row : (y + 1) * row])
        self._push_buffer(buf, pts_ns)

    def _hardware_pool(self):
        """The pool the element after the appsrc offers for its input, if its
        buffers are not system memory, activated; otherwise None."""
        caps = self.src.get_property("caps")
        query = Gst.Query.new_allocation(caps, True)
        if not self.src.get_static_pad("src").peer_query(query) or query.get_n_allocation_pools() == 0:
            return None
        pool, size, min_buffers, max_buffers = query.parse_nth_allocation_pool(0)
        if pool is None:
            return None
        config = pool.get_config()
        Gst.BufferPool.config_set_params(config, caps, size, min_buffers, max_buffers)
        Gst.BufferPool.config_add_option(config, GstVideo.BUFFER_POOL_OPTION_VIDEO_META)
        if not pool.set_config(config) or not pool.set_active(True):
            return None
        ret, buf = pool.acquire_buffer(None)
        if ret != Gst.FlowReturn.OK or buf.peek_memory(0).is_type(Gst.ALLOCATOR_SYSMEM):
            pool.set_active(False)
            return None
        return pool

    def _push_buffer(self, buf, pts_ns):
        buf.pts = PTS_BASE + pts_ns
        self.started = True
        ret = self.src.push_buffer(buf)
        if ret != Gst.FlowReturn.OK:
            raise RuntimeError(f"failed to push a buffer ({ret}): {_bus_error(self.pipeline)}")

    def pull(self, pts_ns, timeout_ms):
        """The output of the buffer pushed with `pts_ns`, if it comes within
        `timeout_ms`, and whether outputs of earlier buffers, which came too
        late, were dropped first."""
        deadline = time.monotonic() + timeout_ms / 1000
        dropped_late = False
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None, dropped_late
            sample = self.sink.try_pull_sample(int(left * Gst.SECOND))
            if sample is None:
                return None, dropped_late
            buf = sample.get_buffer()
            if buf is not None and buf.pts == PTS_BASE + pts_ns:
                return sample, dropped_late
            dropped_late = True

    def force_keyframe(self):
        """Asks the encoder to make the next frame pushed a keyframe. The
        first frame is a keyframe anyway, and qtic2venc crashes on the
        action before it has a frame, so before the first frame this does
        nothing."""
        if not self.started:
            return
        if self.uses_iframe_action:
            self.enc.emit(IFRAME_TRIGGER_SIGNAL)
        else:
            event = GstVideo.video_event_new_downstream_force_key_unit(
                Gst.CLOCK_TIME_NONE, Gst.CLOCK_TIME_NONE, Gst.CLOCK_TIME_NONE, True, 0
            )
            self.src.send_event(event)

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)
        if self._pool not in (None, _UNQUERIED):
            self._pool.set_active(False)
