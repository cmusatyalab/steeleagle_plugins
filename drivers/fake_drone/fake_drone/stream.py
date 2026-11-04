"""Replays a looping .mp4 file as if it were a drone's live camera feed.
Implements only StreamService.StreamCameras -- there's no real vehicle, so
telemetry, control and calibration don't exist here. Decoding reuses
parrot_anafi's GstCapture and RawFramePublisher (see capture.py,
raw_frames.py) -- the only differences from the real driver are forced by
the source being a file instead of a live RTSP stream: GstCapture here paces
playback to the file's own timestamps and loops it on end-of-file, since a
live stream is already paced by the network/camera and never ends.
"""
import logging
import threading

import grpc

import steeleagle_protocol.v1.messages.telemetry.telemetry_pb2 as telemetry_proto
import steeleagle_protocol.v1.services.driver.stream_pb2 as stream_proto
from steeleagle_protocol.v1.services.driver.stream_pb2_grpc import StreamServiceServicer

from fake_drone.capture import GstCapture, file_source
from fake_drone.raw_frames import RawFramePublisher

logger = logging.getLogger('fake-drone/stream')

# Name of the fake camera, matching fake_drone.plugin.toml
CAMERA_NAME = 'rgb'


def _no_pose():
    """fake_drone has no real pose; RawFramePublisher requires something."""
    return telemetry_proto.PositionInfo(), telemetry_proto.GimbalInfo()


class FilePublisher:
    """Decodes `video_path` with GstCapture, using `decoder` and paced to the
    file's own framerate, and publishes every frame with a
    RawFramePublisher -- exactly as parrot_anafi does for its live feed."""

    def __init__(self, uri, video_path, decoder):
        self._capture = GstCapture(file_source(video_path), decoder)
        self._publisher = RawFramePublisher(uri, dimensions=None, pose=_no_pose)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        if not self._capture.isOpened():
            logger.error('could not open video for decoding')
            return
        while not self._stop.is_set():
            ok, rgb = self._capture.read()
            if not ok:
                break
            self._publisher.publish(rgb)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._capture.release()
        self._publisher.close()


class Stream(StreamServiceServicer):
    """StreamService implementation for the fake drone."""

    def __init__(self, video_path, decoder):
        self.video_path = video_path
        self.decoder = decoder
        self._active = None

    def StreamCameras(self, request, context):
        # The fake drone has one camera, so it is streamed if requested, and
        # stopped otherwise. Any other camera is an error.
        requests = list(request.secondaries)
        if request.HasField('primary'):
            requests.append(request.primary)
        unknown = [r.id for r in requests if r.id != CAMERA_NAME]
        if unknown:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f'unknown cameras {unknown}, only {CAMERA_NAME!r} exists',
            )
        if self._active is not None:
            self._active.close()
            self._active = None
        if not requests:
            return stream_proto.StreamCamerasResponse()
        camera = requests[0]
        self._active = FilePublisher(camera.uri, self.video_path, self.decoder)
        return stream_proto.StreamCamerasResponse()

    def StreamTelemetry(self, request, context):
        # No real vehicle, so there's no telemetry to publish; acknowledge
        # successfully without starting anything.
        return stream_proto.StreamTelemetryResponse()
