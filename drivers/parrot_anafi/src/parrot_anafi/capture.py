"""Reads and decodes the drone's video stream through GStreamer.

The stream is H.264 over RTSP; the decoder is GStreamer elements, in
gst-launch syntax, so that a hardware decoder can replace the default software
one. The native environment adds the device's own GStreamer plugins, which hold
such decoders, e.g. `qtic2vdec`.

Frames are converted to RGB on the CPU here, not in GStreamer: videoconvert
can't negotiate GBM memory, but a mapped GBM buffer reads like any other, so a
hardware decoder needs no hardware conversion after it.

Each frame's video metadata (vmeta), the drone's pose when it was captured, is
read from the RTP packets before the depayloader, which drops it, and matched
to the decoded frame by its timestamp.
"""
import collections
import functools
import logging
import threading

import cv2
import gi
import numpy as np

gi.require_version('Gst', '1.0')
gi.require_version('GstApp', '1.0')
gi.require_version('GstRtp', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GstApp, GstRtp, GstVideo  # noqa: E402,F401

from parrot_anafi.vmeta import read_rtp_extension  # noqa: E402

Gst.init(None)

logger = logging.getLogger('parrot-anafi/capture')

# Frames are decoded to RGB, one byte per channel.
RGB_CHANNELS = 3
# Default H.264 decoder, matching anafi4k.plugin.toml.
DEFAULT_DECODER = 'avdec_h264 max-threads=1'
# What the drone's live stream is, from its SPS: 1280x720 at 29.97 fps, 8-bit
# 4:2:0. h264parse leaves these out of its caps on this stream, and Qualcomm's
# qtic2vdec refuses caps without them, so they are set after it. Software
# decoders ignore them. The stream is byte-stream with one frame per buffer,
# the only form qtic2vdec takes.
STREAM_CAPS = (
    'video/x-h264,stream-format=byte-stream,alignment=au '
    '! capssetter caps="video/x-h264,width=(int)1280,height=(int)720,'
    'framerate=(fraction)30000/1001,chroma-format=(string)4:2:0,'
    'bit-depth-luma=(uint)8,bit-depth-chroma=(uint)8"'
)
# Seconds the pipeline may take to start.
OPEN_TIMEOUT = 10.0
# Seconds a read waits for a frame before it fails.
READ_TIMEOUT = 1.0
# Name of the source's RTP depayloader, whose packets carry frame metadata.
DEPAYLOADER_NAME = 'depay'
# Frames whose metadata is kept until they are decoded; decoding lags the
# packets by a few frames at most.
MAX_PENDING_METADATA = 64
# Nanoseconds by which a frame's metadata may precede it: its packets arrive
# within a frame interval, and so are timestamped within one.
MAX_METADATA_LEAD = 30 * Gst.MSECOND


def rtsp_source(ip):
    """The drone's H.264 stream, as GStreamer elements ending in H.264.

    The drone's DESCRIBE answers with Content-Base rtsp://{ip}/live, with no
    trailing slash, and a=control:front. rtspsrc 1.28 resolves that as a URL,
    to rtsp://{ip}/front, which the drone answers with 500 and rtspsrc does
    not retry; the stream is at rtsp://{ip}/live/front.
    force-non-compliant-url makes rtspsrc append the control to the base
    instead, as older versions always did; those versions, such as the
    device's own 1.16 in the native environment, lack the property.

    The depayloader is named DEPAYLOADER_NAME, so that GstCapture reads the
    frame metadata in its packets."""
    compat = 'force-non-compliant-url=true ' if _rtspsrc_has_non_compliant_url() else ''
    return (f'rtspsrc location=rtsp://{ip}/live protocols=udp latency=0 '
            f'{compat}! rtph264depay name={DEPAYLOADER_NAME}')


@functools.cache
def _rtspsrc_has_non_compliant_url():
    """Whether the rtspsrc GStreamer loads has force-non-compliant-url."""
    src = Gst.ElementFactory.make('rtspsrc')
    return src is not None and src.find_property('force-non-compliant-url') is not None


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
    """Decodes frames from `source` with `decoder`, both in gst-launch syntax,
    into RGB arrays, converting the decoder's output on the CPU; see to_rgb
    for the formats it may be in. It reads like an OpenCV VideoCapture:
    isOpened, read and release. Only the latest frame is kept, so a slow
    reader skips frames rather than falling behind.

    If `source` ends in an RTP depayloader named DEPAYLOADER_NAME, the frame
    metadata in its packets is read, and `metadata` is that of the frame the
    last read gave, as vmeta.read_rtp_extension gives it, or None if it had
    none."""

    def __init__(self, source, decoder):
        description = (
            f'{source} ! h264parse ! {STREAM_CAPS} ! {decoder} '
            f'! appsink name=sink max-buffers=1 drop=true sync=false'
        )
        self._opened = False
        self._logged_format = None
        self.metadata = None
        # Metadata of frames not yet read, by timestamp, oldest first. The
        # depayloader's streaming thread adds to it, and reads take from it.
        self._pending_metadata = collections.OrderedDict()
        self._metadata_lock = threading.Lock()
        try:
            self._pipeline = Gst.parse_launch(description)
        except Exception as e:
            logger.error(f'could not build pipeline {description!r}: {e}')
            self._pipeline = None
            return
        self._sink = self._pipeline.get_by_name('sink')
        depayloader = self._pipeline.get_by_name(DEPAYLOADER_NAME)
        if depayloader is not None:
            depayloader.get_static_pad('sink').add_probe(
                Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST, self._on_packets)
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
        self.metadata = self._take_metadata(sample.get_buffer().pts)
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
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return False, None
        try:
            frame = to_rgb(mapinfo.data, pixel_format, width, height, offsets, strides)
        except ValueError as e:
            logger.warning(f'could not convert a frame: {e}')
            return False, None
        finally:
            buf.unmap(mapinfo)
        return True, frame

    def release(self):
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        self._opened = False

    def _on_packets(self, pad, info):
        """Keeps the metadata of the RTP packets about to be depayloaded."""
        if info.type & Gst.PadProbeType.BUFFER_LIST:
            packets = info.get_buffer_list()
            for i in range(packets.length()):
                self.add_packet(packets.get(i))
        else:
            self.add_packet(info.get_buffer())
        return Gst.PadProbeReturn.OK

    def add_packet(self, packet):
        """Keeps the frame metadata in the header extension of `packet`, an
        RTP packet, if it has any, for the frame of its timestamp. A frame's
        packets share its RTP timestamp, so their buffers' timestamps are
        within the frame's interval."""
        if packet.pts == Gst.CLOCK_TIME_NONE:
            return
        ok, rtp = GstRtp.RTPBuffer.map(packet, Gst.MapFlags.READ)
        if not ok:
            return
        try:
            if not rtp.get_extension():
                return
            data, profile = rtp.get_extension_data()
            if data is None:
                return
            meta = read_rtp_extension(profile, data.get_data())
        finally:
            rtp.unmap()
        if meta is None:
            return
        with self._metadata_lock:
            # Later packets of a frame may repeat its metadata.
            self._pending_metadata[packet.pts] = meta
            while len(self._pending_metadata) > MAX_PENDING_METADATA:
                self._pending_metadata.popitem(last=False)

    def _take_metadata(self, pts):
        """The metadata of the frame decoded with the timestamp `pts`, or None,
        dropping that of it and of frames before it. A frame's metadata is
        that of the latest packets at or before it, if they are close enough
        to be its own; the stream has no B-frames, so frames are decoded in
        the order their packets came."""
        if pts == Gst.CLOCK_TIME_NONE:
            return None
        meta = None
        with self._metadata_lock:
            while self._pending_metadata:
                first = next(iter(self._pending_metadata))
                if first > pts:
                    break
                _, meta = self._pending_metadata.popitem(last=False)
                meta_pts = first
        if meta is None or pts - meta_pts > MAX_METADATA_LEAD:
            return None
        return meta

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
