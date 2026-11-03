"""Replays a directory of pre-captured raw RGB frames as if they were a
drone's live camera feed. Implements only StreamService.StreamCameras --
there's no real vehicle, so telemetry, control and calibration don't exist
here. Modeled on parrot_anafi's Stream/RawFramePublisher, minus the drone.
"""
import logging
import threading
import time
from pathlib import Path

import grpc
import uriel
from google.protobuf.timestamp_pb2 import Timestamp

import steeleagle_protocol.v1.messages.frame.frame_pb2 as frame_proto
import steeleagle_protocol.v1.services.driver.stream_pb2 as stream_proto
from steeleagle_protocol.v1.services.driver.stream_pb2_grpc import StreamServiceServicer

logger = logging.getLogger('fake-drone/stream')

# Name of the fake camera, matching fake_drone.plugin.toml
CAMERA_NAME = 'rgb'
RGB_CHANNELS = 3
# Room in an input for a RawFrame's fields besides its pixels
RAW_FRAME_HEADROOM = 16 << 10
RAW_FRAME_TYPE = 'steeleagle_protocol/v1/messages/frame/RawFrame'


class FramePublisher:
    """Publishes RawFrames read from a directory of .rgb files, looping
    forever at a fixed fps, as the Uriel producer `uri`. The producer is
    created on the thread that uses it -- Uriel objects must stay on the
    thread that created them (same rule as run_producer.py's Subscription)."""

    def __init__(self, uri, frames_dir, h_res, v_res, fps):
        self.uri = uri
        self._frames = sorted(Path(frames_dir).glob('*.rgb'))
        if not self._frames:
            raise RuntimeError(f'no .rgb frames found in {frames_dir}')
        self._h_res = h_res
        self._v_res = v_res
        self._period = 1.0 / fps
        self._next_id = 0
        self._stop = threading.Event()
        self._thread = None
        logger.info('will publish raw frames as %s, looping %d frames from %s',
                    uri, len(self._frames), frames_dir)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        frame_size = self._h_res * self._v_res * RGB_CHANNELS
        producer = uriel.Producer(
            self.uri, input_type=RAW_FRAME_TYPE, max_input_size=frame_size + RAW_FRAME_HEADROOM,
        )
        try:
            i = 0
            while not self._stop.is_set():
                started = time.monotonic()
                path = self._frames[i % len(self._frames)]
                data = path.read_bytes()
                timestamp = Timestamp()
                timestamp.GetCurrentTime()
                frame = frame_proto.RawFrame(
                    timestamp=timestamp,
                    data=data,
                    h_res=self._h_res,
                    v_res=self._v_res,
                    channels=RGB_CHANNELS,
                    id=self._next_id,
                )
                self._next_id += 1
                i += 1
                payload = frame.SerializeToString()
                try:
                    producer.send(payload)
                except uriel.AgainError:
                    logger.debug('no free input buffer; dropping frame %d', frame.id)
                sleep_left = self._period - (time.monotonic() - started)
                if sleep_left > 0:
                    time.sleep(sleep_left)
        finally:
            producer.close()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

class Stream(StreamServiceServicer):
    """StreamService implementation for the fake drone."""

    def __init__(self, frames_dir, fps, h_res=1280, v_res=720):
        self.frames_dir = frames_dir
        self.fps = fps
        self.h_res = h_res
        self.v_res = v_res
        self._publisher = None

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
        if self._publisher is not None:
            self._publisher.close()
            self._publisher = None
        if not requests:
            return stream_proto.StreamCamerasResponse()
        camera = requests[0]
        self._publisher = FramePublisher(camera.uri, self.frames_dir, self.h_res, self.v_res, self.fps)
        self._publisher.start()
        return stream_proto.StreamCamerasResponse()
    
    def StreamTelemetry(self, request, context):
    	# No real vehicle, so there's no telemetry to publish; acknowledge
    	# successfully without starting anything.
    	return stream_proto.StreamTelemetryResponse()
