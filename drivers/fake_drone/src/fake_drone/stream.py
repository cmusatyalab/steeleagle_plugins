"""Stream Service of the fake drone: replays an .mp4 file as its camera,
looping it, or, with no_loop, playing it once and then stopping the plugin,
or, with pad_after too, sending blank frames at 30 fps after it ends.

The vehicle calls StreamCameras when a flow is pushed, so the video plays
from its start each time the camera is asked for, as an experiment's trial
expects, and stops when it is no longer asked for (e.g. after a push fails
with no flow set). Each frame is published to the producer the vehicle asked
for. With telemetry on, each frame carries the position and gimbal
pose a Parrot drone recorded with it (see vmeta.py). There's no vehicle, so
there's no telemetry stream: StreamTelemetry starts nothing.
"""
import logging
import threading
import time

import numpy as np

import grpc

import steeleagle_protocol.v1.services.driver.stream_pb2 as stream_proto
from steeleagle_protocol.v1.services.driver.stream_pb2_grpc import StreamServiceServicer

from fake_drone.capture import GstCapture
from fake_drone.raw_frames import RawFramePublisher

logger = logging.getLogger('fake-drone/stream')

# Seconds to wait before reopening the video, after it failed to open or
# stopped giving frames.
CAPTURE_RETRY_DELAY = 1.0
# Failed reads in a row after which the video is reopened. A read fails at
# once on a broken pipeline, or after GstCapture's read timeout on a stuck one.
MAX_FAILED_READS = 10
# Name of the fake camera, matching fake_drone.plugin.toml
CAMERA_NAME = 'rgb'
# Framerate of the blank frames sent after the video ends, with pad_after
PAD_FPS = 30
# (height, width) of the blank frames if the video gave none to match, as
# the camera's resolution in fake_drone.plugin.toml
PAD_DEFAULT_SHAPE = (720, 1280)

RESOLUTION_DIMENSIONS = {
    stream_proto.RESOLUTION_360P: (640, 360),
    stream_proto.RESOLUTION_480P: (854, 480),
    stream_proto.RESOLUTION_720P: (1280, 720),
    stream_proto.RESOLUTION_1080P: (1920, 1080),
    stream_proto.RESOLUTION_4K: (3840, 2160),
}


class Stream(StreamServiceServicer):
    """Stream Service implementation. `telemetry` is the vmeta.Telemetry
    recorded in the video, which goes with each frame, or None. Unless
    `loop`, the video plays once, and then `on_end` is called, or, if
    `pad_after`, blank frames are sent at PAD_FPS until the playback is
    stopped."""

    def __init__(self, video_path, decoder, telemetry=None, loop=True, on_end=None,
                 pad_after=False):
        self.video_path = video_path
        # H.264 decoder of the video, in gst-launch syntax
        self.decoder = decoder
        self.telemetry = telemetry
        self.loop = loop
        self.on_end = on_end
        self.pad_after = pad_after
        # (height, width) of the last frame read, which blank frames match
        self._frame_shape = None
        self._capture_lock = threading.Lock()
        self._grabber_thread = None
        # Set to stop the current playback
        self._stop = threading.Event()
        # The camera stream the vehicle asked for, as (uri, dimensions), or
        # None. The reading thread publishes it, since the publisher must
        # stay on one thread, so it is only handed over here.
        self._camera_request = None
        self._raw_frames = None

    def _stop_capture(self):
        """Stops the current playback, if any, and waits for it to end. The
        caller must hold _capture_lock."""
        if self._grabber_thread is None:
            return
        self._stop.set()
        self._grabber_thread.join()
        self._grabber_thread = None

    def _restart_capture(self):
        """Plays the video from its start, stopping the current playback."""
        with self._capture_lock:
            self._stop_capture()
            self._stop = threading.Event()
            self._grabber_thread = threading.Thread(
                target=self._grab_frames, args=(self._stop,), daemon=True)
            self._grabber_thread.start()

    def _grab_frames(self, stop):
        """Reads the video until `stop` is set, reopening it whenever it fails
        to open or stops giving frames, or, unless looping, until it ends, and
        then calls on_end, or, with pad_after, sends blank frames until `stop`
        is set. The publisher is used only here, on this thread.
        """
        ended = False
        padded = False
        try:
            while not stop.is_set():
                cap = GstCapture(self.video_path, self.decoder, loop=self.loop)
                if cap.isOpened():
                    logger.info('playing the video from its start')
                    self._read_frames(cap, stop)
                    if cap.ended:
                        ended = True
                        cap.release()
                        break
                    if stop.is_set():
                        cap.release()
                        break
                    logger.warning(f'video stopped; reopening it in {CAPTURE_RETRY_DELAY} s')
                else:
                    logger.warning(f'video could not be opened; retrying in {CAPTURE_RETRY_DELAY} s')
                cap.release()
                stop.wait(CAPTURE_RETRY_DELAY)
            if ended and self.pad_after:
                padded = True
                self._send_blank_frames(stop)
        finally:
            if self._raw_frames is not None:
                self._raw_frames.close()
                self._raw_frames = None
        if not ended or padded:
            logger.info('video stopped')
            return
        logger.info('video ended')
        if self.on_end is not None:
            self.on_end()

    def _update_publisher(self):
        """Replaces the publisher if the vehicle asked for another camera
        stream since the last frame, or closes it if it asked for none."""
        request = self._camera_request
        current = None
        if self._raw_frames is not None:
            current = (self._raw_frames.uri, self._raw_frames.dimensions)
        if request == current:
            return
        if self._raw_frames is not None:
            self._raw_frames.close()
            self._raw_frames = None
        if request is not None:
            uri, dimensions = request
            self._raw_frames = RawFramePublisher(uri, dimensions)

    def _read_frames(self, cap, stop):
        """Reads frames from `cap`, and publishes each one to Uriel, if the
        vehicle asked for the camera. Returns once MAX_FAILED_READS reads in
        a row fail, or `stop` is set.
        """
        failed_reads = 0
        while failed_reads < MAX_FAILED_READS and not stop.is_set():
            ret, frame, pts = cap.read()
            if not ret:
                if cap.ended:
                    return
                failed_reads += 1
                continue
            failed_reads = 0
            self._frame_shape = frame.shape[:2]
            self._update_publisher()
            if self._raw_frames is None:
                continue
            position_info = gimbal_info = None
            if self.telemetry is not None and pts is not None:
                position_info, gimbal_info = self.telemetry.pose(pts)
            try:
                self._raw_frames.publish(frame, position_info, gimbal_info)
            except Exception as e:
                logger.warning(f'raw frame could not be published, reason: {e}')

    def _send_blank_frames(self, stop):
        """Publishes black frames, the size of the video's, at PAD_FPS until
        `stop` is set, if the vehicle asks for the camera."""
        height, width = self._frame_shape or PAD_DEFAULT_SHAPE
        blank = np.zeros((height, width, 3), dtype=np.uint8)
        logger.info(f'video ended; sending {width}x{height} blank frames at {PAD_FPS} fps')
        period = 1.0 / PAD_FPS
        deadline = time.monotonic()
        while not stop.is_set():
            self._update_publisher()
            if self._raw_frames is not None:
                try:
                    self._raw_frames.publish(blank)
                except Exception as e:
                    logger.warning(f'blank frame could not be published, reason: {e}')
            deadline += period
            delay = deadline - time.monotonic()
            if delay < 0:
                # Fell behind, e.g. on a slow publish: skip the missed frames
                # rather than sending them in a burst
                deadline = time.monotonic()
                delay = 0
            stop.wait(delay)

    def StreamCameras(self, request, context):
        # The fake drone has one camera, so it is streamed if it is requested,
        # and stopped otherwise. Any other camera is an error.
        requests = list(request.secondaries)
        if request.HasField('primary'):
            requests.append(request.primary)
        unknown = [r.id for r in requests if r.id != CAMERA_NAME]
        if unknown:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f'unknown cameras {unknown}, only {CAMERA_NAME!r} exists',
            )
        if not requests:
            self._camera_request = None
            with self._capture_lock:
                self._stop_capture()
            return stream_proto.StreamCamerasResponse()
        camera = requests[0]
        dimensions = None
        if camera.HasField('target_resolution'):
            dimensions = RESOLUTION_DIMENSIONS.get(camera.target_resolution)
        self._camera_request = (camera.uri, dimensions)
        self._restart_capture()
        return stream_proto.StreamCamerasResponse()

    def StreamTelemetry(self, request, context):
        # No vehicle, so no telemetry stream; frames carry their own pose.
        return stream_proto.StreamTelemetryResponse()
