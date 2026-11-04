"""Reads and decodes a looping .mp4 file through GStreamer, standing in for
parrot_anafi's RTSP drone feed. The decode chain and RGB stride handling are
unchanged from parrot_anafi's GstCapture. Differences from parrot_anafi's
original are limited to what's forced by the source being a file instead of
a live RTSP stream: STREAM_CAPS' width/height/framerate match this file
(confirmed via gst-launch -v) instead of the drone's 1280x720 stream, sync=
true paces playback to the file's own timestamps (a live RTSP source is
already paced by the network/camera, so parrot_anafi doesn't need this), and
end-of-file triggers a seek back to the start, in place, since a live stream
never ends and the pipeline must keep running without a gap.
"""
import logging

import gi
import numpy as np

gi.require_version('Gst', '1.0')
gi.require_version('GstApp', '1.0')
from gi.repository import Gst, GstApp  # noqa: E402,F401

Gst.init(None)

logger = logging.getLogger('fake-drone/capture')

RGB_CHANNELS = 3
# Default H.264 decoder: the Qualcomm hardware decoder fermi actually runs,
# matching parrot_anafi's real decode path.
DEFAULT_DECODER = 'qtic2vdec ! qtivcrop crop-type=FastCV crop=<0,0,1920,1080>'
# qtic2vdec refuses caps without chroma-format/bit-depth, which h264parse
# doesn't carry through from this file (confirmed via gst-launch -v: its src
# caps have width/height/framerate but no chroma-format/bit-depth-luma/
# bit-depth-chroma) -- same requirement parrot_anafi's STREAM_CAPS satisfies
# for the drone's RTSP stream, just with this file's own resolution/
# framerate instead of the drone's 1280x720. Software decoders ignore these
# extra fields.
STREAM_CAPS = (
    'video/x-h264,stream-format=byte-stream,alignment=au '
    '! capssetter caps="video/x-h264,width=(int)1920,height=(int)1080,'
    'framerate=(fraction)30000/1001,chroma-format=(string)4:2:0,'
    'bit-depth-luma=(uint)8,bit-depth-chroma=(uint)8"'
)
OPEN_TIMEOUT = 10.0
READ_TIMEOUT = 1.0


def file_source(path):
    """A looping .mp4 file, as GStreamer elements ending in H.264."""
    return f'filesrc location={path} ! qtdemux'


def _rgb_stride(width):
    """GStreamer pads each row of an RGB frame to a multiple of 4 bytes."""
    return (width * RGB_CHANNELS + 3) & ~3


class GstCapture:
    """Decodes frames from `source` with `decoder` into RGB arrays, paced to
    the source's own timestamps. Identical to parrot_anafi's GstCapture
    except sync=true on the sink (see module docstring)."""

    def __init__(self, source, decoder):
        description = (
            f'{source} ! h264parse ! {STREAM_CAPS} ! {decoder} '
            f'! videoconvert '
            f'! video/x-raw,format=RGB '
            f'! appsink name=sink max-buffers=1 drop=true sync=true'
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
        """(True, an RGB frame) once one is decoded, paced to the file's own
        framerate; loops to the start on end-of-file instead of failing."""
        if not self._opened:
            return False, None
        error = self._bus_error()
        if error == 'end of stream':
            if not self._pipeline.seek_simple(
                Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0,
            ):
                logger.warning('could not loop %s', self._pipeline)
                self._opened = False
                return False, None
        elif error is not None:
            logger.warning(f'video decode failed: {error}')
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
