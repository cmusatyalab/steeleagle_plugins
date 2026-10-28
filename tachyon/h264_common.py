"""Shared GStreamer plumbing for h264-encode / h264-decode: a pipeline an
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

FRAME_W = 1280
FRAME_H = 720
RGB_FRAME_SIZE = FRAME_W * FRAME_H * 3

# Where a pipeline's timestamps start. An encoder based on GstVideoEncoder
# shifts every timestamp by 1000 hours when a decoding timestamp would
# otherwise be negative, as it would be near 0; timestamps that start there
# are never shifted, so outputs keep their inputs' stamps.
PTS_BASE = 1000 * 60 * 60 * Gst.SECOND
IFRAME_TRIGGER_SIGNAL = "trigger-iframe"


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

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError(f"failed to start the pipeline: {_bus_error(self.pipeline)}")

    def push(self, data, pts_ns):
        """Pushes the bytes `data`, stamped `pts_ns` (int nanoseconds),
        which must differ from the stamps of other buffers."""
        buf = Gst.Buffer.new_wrapped(data)
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
            # NOTE: least-verified call in this file -- if this errors,
            # paste the traceback.
            event = GstVideo.video_event_new_downstream_force_key_unit(
                Gst.CLOCK_TIME_NONE, Gst.CLOCK_TIME_NONE, Gst.CLOCK_TIME_NONE, True, 0
            )
            self.src.send_event(event)

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)
