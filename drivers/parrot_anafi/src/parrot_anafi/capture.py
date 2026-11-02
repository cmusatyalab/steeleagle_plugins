"""Reads and decodes the drone's video stream through GStreamer.

The stream is H.264 over RTSP; the decoder is GStreamer elements, in
gst-launch syntax, so that a hardware decoder can replace the default software
one. A hardware decoder whose frames are in hardware memory must copy them
into system memory itself, e.g. in the qtic environment
`qtivdec ! qtivtransform ! video/x-raw,format=RGB`, since videoconvert can't
read GBM memory.
"""
import logging

import gi
import numpy as np

gi.require_version('Gst', '1.0')
gi.require_version('GstApp', '1.0')
from gi.repository import Gst, GstApp  # noqa: E402,F401

Gst.init(None)

logger = logging.getLogger('parrot-anafi/capture')

# Frames are decoded to RGB, one byte per channel.
RGB_CHANNELS = 3
# Default H.264 decoder, matching anafi4k.plugin.toml.
DEFAULT_DECODER = 'avdec_h264 max-threads=1'
# Seconds the pipeline may take to start.
OPEN_TIMEOUT = 10.0
# Seconds a read waits for a frame before it fails.
READ_TIMEOUT = 1.0


def rtsp_source(ip):
    """The drone's H.264 stream, as GStreamer elements ending in H.264."""
    return (f'rtspsrc location=rtsp://{ip}/live protocols=udp latency=0 '
            f'! rtph264depay')


def _rgb_stride(width):
    """GStreamer pads each row of an RGB frame to a multiple of 4 bytes."""
    return (width * RGB_CHANNELS + 3) & ~3


class GstCapture:
    """Decodes frames from `source` with `decoder`, both in gst-launch syntax,
    into RGB arrays. The decoder's output must be in system memory; it is
    always converted to RGB after it, since SteelEagle expects RGB RawFrames,
    which is a passthrough if the decoder already outputs RGB. It reads like
    an OpenCV VideoCapture: isOpened, read and release. Only the latest frame
    is kept, so a slow reader skips frames rather than falling behind."""

    def __init__(self, source, decoder):
        description = (
            f'{source} ! h264parse ! {decoder} '
            f'! videoconvert '
            f'! video/x-raw,format=RGB '
            f'! appsink name=sink max-buffers=1 drop=true sync=false'
        )
        self._opened = False
        try:
            self._pipeline = Gst.parse_launch(description)
        except Exception as e:
            logger.error(f'could not build pipeline {description!r}: {e}')
            self._pipeline = None
            return
        self._sink = self._pipeline.get_by_name('sink')
        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret != Gst.StateChangeReturn.FAILURE:
            ret, _, _ = self._pipeline.get_state(int(OPEN_TIMEOUT * Gst.SECOND))
        if ret == Gst.StateChangeReturn.FAILURE:
            logger.warning(f'pipeline failed to start: {self._bus_error() or "no error given"}')
            return
        self._opened = True

    def isOpened(self):
        return self._opened

    def read(self):
        """(True, an RGB frame) once one is decoded, or (False, None) if none
        is within READ_TIMEOUT, or the pipeline stopped."""
        if not self._opened:
            return False, None
        error = self._bus_error()
        if error is not None:
            logger.warning(f'video stream failed: {error}')
            self._opened = False
            return False, None
        sample = self._sink.try_pull_sample(int(READ_TIMEOUT * Gst.SECOND))
        if sample is None:
            return False, None
        caps = sample.get_caps().get_structure(0)
        _, width = caps.get_int('width')
        _, height = caps.get_int('height')
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return False, None
        try:
            stride = _rgb_stride(width)
            rows = np.frombuffer(mapinfo.data, dtype=np.uint8, count=stride * height)
            frame = rows.reshape(height, stride)[:, :width * RGB_CHANNELS]
            frame = frame.reshape(height, width, RGB_CHANNELS).copy()
        finally:
            buf.unmap(mapinfo)
        return True, frame

    def release(self):
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        self._opened = False

    def _bus_error(self):
        """The pipeline's error, or None; end of stream counts as one."""
        bus = self._pipeline.get_bus() if self._pipeline is not None else None
        if bus is None:
            return None
        msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if msg is None:
            return None
        if msg.type == Gst.MessageType.EOS:
            return 'end of stream'
        err, debug = msg.parse_error()
        return f'{err} ({debug})' if debug else str(err)
