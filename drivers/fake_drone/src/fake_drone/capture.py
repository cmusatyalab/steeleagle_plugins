"""Reads and decodes a looping .mp4 file through GStreamer, as if it were a
drone's live camera.

This is parrot_anafi's GstCapture, reading a file instead of the drone's RTSP
stream. The decoder is GStreamer elements, in gst-launch syntax, so that a
hardware decoder can replace the default software one; the native environment
adds the device's own GStreamer plugins, which hold such decoders, e.g.
`qtic2vdec`.

As in parrot_anafi, frames are converted to RGB on the CPU here, not in
GStreamer: videoconvert can't negotiate GBM memory, but a mapped GBM buffer
reads like any other, so a hardware decoder needs no hardware conversion after
it.

What differs from a live stream: the sink syncs to the clock, so frames come
at the file's own framerate, as a camera would give them, and at the end of
the file the pipeline seeks back to its start, since a camera never ends.
"""
import logging

import cv2
import gi
import numpy as np

gi.require_version('Gst', '1.0')
gi.require_version('GstApp', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GstApp, GstVideo  # noqa: E402,F401

from fake_drone import mp4  # noqa: E402

Gst.init(None)

logger = logging.getLogger('fake-drone/capture')

# Frames are decoded to RGB, one byte per channel.
RGB_CHANNELS = 3
# Default H.264 decoder, matching fake_drone.plugin.toml.
DEFAULT_DECODER = 'avdec_h264'
# Seconds the pipeline may take to start.
OPEN_TIMEOUT = 10.0
# Seconds a read waits for a frame before it fails.
READ_TIMEOUT = 1.0


def stream_caps(path):
    """The caps of the file's H.264 stream after h264parse, in gst-launch
    syntax: byte-stream with one frame per buffer, the only form qtic2vdec
    takes. Qualcomm's qtic2vdec also refuses caps without the dimensions,
    framerate, chroma format and bit depth, so they are set from the file's
    video track when it can be read; software decoders ignore them. The
    stream is taken to be 8-bit 4:2:0, as drones record."""
    caps = 'video/x-h264,stream-format=byte-stream,alignment=au'
    try:
        movie = mp4.read_movie(path)
    # Any failure, e.g. a truncated box, leaves a bad file for GStreamer to report
    except Exception as e:
        logger.warning(f'could not read the video track of {path}: {e}')
        return caps
    video = next((t for t in movie.tracks if t.handler == 'vide'), None)
    if video is None or not video.width or not video.sample_duration:
        return caps
    return (
        f'{caps} ! capssetter caps="video/x-h264,'
        f'width=(int){video.width},height=(int){video.height},'
        f'framerate=(fraction){video.timescale}/{video.sample_duration},'
        f'chroma-format=(string)4:2:0,bit-depth-luma=(uint)8,bit-depth-chroma=(uint)8"'
    )


def _plane(data, offset, stride, height, width, channels=1):
    """A plane of a frame in `data`, without copying it."""
    shape, strides = (height, width), (stride, 1)
    if channels > 1:
        shape, strides = (height, width, channels), (stride, channels, 1)
    return np.ndarray(shape, dtype=np.uint8, buffer=data, offset=offset, strides=strides)


def to_rgb(data, pixel_format, width, height, offsets, strides):
    """A new RGB array of a frame in `data`, in the GStreamer format
    `pixel_format` (NV12, I420 or RGB), each of whose planes starts at its
    offset in `offsets` with rows its stride in `strides` bytes apart.
    Raises ValueError for any other format. OpenCV converts YUV as BT.601,
    limited range."""
    half_width, half_height = (width + 1) // 2, (height + 1) // 2
    if pixel_format == 'NV12':
        y = _plane(data, offsets[0], strides[0], height, width)
        uv = _plane(data, offsets[1], strides[1], half_height, half_width, channels=2)
        return cv2.cvtColorTwoPlane(y, uv, cv2.COLOR_YUV2RGB_NV12)
    if pixel_format == 'I420':
        y = _plane(data, offsets[0], strides[0], height, width)
        u = _plane(data, offsets[1], strides[1], half_height, half_width)
        v = _plane(data, offsets[2], strides[2], half_height, half_width)
        planar = np.concatenate((y.ravel(), u.ravel(), v.ravel()))
        return cv2.cvtColor(planar.reshape(height * 3 // 2, width), cv2.COLOR_YUV2RGB_I420)
    if pixel_format == 'RGB':
        return _plane(data, offsets[0], strides[0], height, width, RGB_CHANNELS).copy()
    raise ValueError(f'cannot convert {pixel_format} frames to RGB')


def _layout(sample):
    """The offsets and strides of the planes of `sample`'s frame. A decoder
    that pads its frames says so in the buffer's video meta, which is the only
    place the padding is: the caps give the layout of an unpadded frame."""
    meta = GstVideo.buffer_get_video_meta(sample.get_buffer())
    if meta is not None:
        return list(meta.offset), list(meta.stride)
    info = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
    return list(info.offset), list(info.stride)


class GstCapture:
    """Decodes the video of the .mp4 file at `path` with `decoder`, in
    gst-launch syntax, into RGB arrays, converting the decoder's output on the
    CPU; see to_rgb for the formats it may be in. Frames come at the file's
    framerate, and the file loops forever, or, unless `loop`, stops at its
end, after which `ended` is set and reads fail. It reads like an OpenCV
    VideoCapture: isOpened, read and release. Only the latest frame is kept,
    so a slow reader skips frames rather than falling behind, as with a live
    camera."""

    def __init__(self, path, decoder, loop=True):
        description = (
            f'filesrc name=src ! qtdemux name=demux demux.video_0 '
            f'! h264parse ! {stream_caps(path)} ! {decoder} '
            f'! appsink name=sink max-buffers=1 drop=true sync=true'
        )
        self._opened = False
        self._logged_format = None
        # Unless looping, the video stops at its end, and ended is set
        self._loop = loop
        self.ended = False
        try:
            self._pipeline = Gst.parse_launch(description)
        except Exception as e:
            logger.error(f'could not build pipeline {description!r}: {e}')
            self._pipeline = None
            return
        # Set as a property, so that the path needs no gst-launch quoting
        self._pipeline.get_by_name('src').set_property('location', path)
        self._sink = self._pipeline.get_by_name('sink')
        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret != Gst.StateChangeReturn.FAILURE:
            ret, _, _ = self._pipeline.get_state(int(OPEN_TIMEOUT * Gst.SECOND))
        if ret == Gst.StateChangeReturn.FAILURE:
            logger.warning(f'pipeline failed to start: {self._bus_error() or "no error given"}')
            return
        logger.info(f'decoding {path} with {description!r}')
        self._opened = True

    def isOpened(self):
        return self._opened

    def read(self):
        """(True, an RGB frame, its presentation time in seconds into the
        file, or None if it has none) once one is decoded, or (False, None,
        None) if none is within READ_TIMEOUT, or the pipeline stopped."""
        if not self._opened:
            return False, None, None
        error = self._bus_error()
        # The appsink can reach end of stream without the pipeline posting
        # EOS on the bus, so its own state is checked too
        if error is None and self._sink.is_eos():
            error = 'end of stream'
        if error == 'end of stream' and not self._loop:
            logger.info('the video ended')
            self.ended = True
            self._opened = False
            return False, None, None
        if error == 'end of stream':
            if not self._pipeline.seek_simple(
                    Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0):
                logger.warning('could not loop the video back to its start')
                self._opened = False
                return False, None, None
            logger.info('looping the video back to its start')
        elif error is not None:
            logger.warning(f'video decode failed: {error}')
            self._opened = False
            return False, None, None
        sample = self._sink.try_pull_sample(int(READ_TIMEOUT * Gst.SECOND))
        if sample is None:
            return False, None, None
        caps = sample.get_caps().get_structure(0)
        _, width = caps.get_int('width')
        _, height = caps.get_int('height')
        pixel_format = caps.get_string('format')
        if pixel_format != self._logged_format:
            self._logged_format = pixel_format
            logger.info(f'decoder outputs {width}x{height} {pixel_format}: '
                        f'{sample.get_caps().to_string()}')
        offsets, strides = _layout(sample)
        buf = sample.get_buffer()
        pts = None if buf.pts == Gst.CLOCK_TIME_NONE else buf.pts / Gst.SECOND
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return False, None, None
        try:
            frame = to_rgb(mapinfo.data, pixel_format, width, height, offsets, strides)
        except ValueError as e:
            logger.warning(f'could not convert a frame: {e}')
            return False, None, None
        finally:
            buf.unmap(mapinfo)
        return True, frame, pts

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
