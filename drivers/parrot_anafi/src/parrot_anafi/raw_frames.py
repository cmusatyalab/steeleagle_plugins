"""Publishes the drone's decoded video frames to Uriel, as RawFrame inputs.

The vehicle asks for them with StreamCameras, which names the producer to
publish them as. Frames are published from the thread that reads the video
stream, as soon as each is decoded.
"""
import logging

import cv2
import uriel
from google.protobuf.timestamp_pb2 import Timestamp

import steeleagle_protocol.v1.messages.frame.frame_pb2 as frame_proto

logger = logging.getLogger('parrot-anafi/raw-frames')

# Engines read a RawFrame's pixels as RGB, one byte per channel.
RGB_CHANNELS = 3
# Room in an input for a RawFrame's fields besides its pixels: its ids,
# timestamp and pose.
RAW_FRAME_HEADROOM = 16 << 10
# Uriel payload type of the frames, as named in plugin configs.
RAW_FRAME_TYPE = 'steeleagle_protocol/v1/messages/frame/RawFrame'


class RawFramePublisher:
    """Publishes frames as the Uriel producer `uri`.

    Frames are resized to `dimensions` (width, height), if given. `pose`
    returns the vehicle's PositionInfo and GimbalInfo, which go with each
    frame. The producer is created on the first frame, large enough for it,
    so a publisher must be used, and closed, on one thread.
    """

    def __init__(self, uri, dimensions, pose):
        self.uri = uri
        self.dimensions = dimensions
        self._pose = pose
        self._producer = None
        self._max_input_size = 0
        self._next_id = 0

    def publish(self, bgr_frame):
        """Publishes `bgr_frame`, an image as OpenCV decodes it (BGR). A frame
        that finds no free input buffer is dropped: newer frames follow."""
        if self.dimensions is not None and bgr_frame.shape[1::-1] != self.dimensions:
            bgr_frame = cv2.resize(bgr_frame, self.dimensions, interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        timestamp = Timestamp()
        timestamp.GetCurrentTime()
        position_info, gimbal_info = self._pose()
        frame = frame_proto.RawFrame(
            timestamp=timestamp,
            data=rgb.tobytes(),
            h_res=rgb.shape[1],
            v_res=rgb.shape[0],
            channels=RGB_CHANNELS,
            id=self._next_id,
            position_info=position_info,
            gimbal_info=gimbal_info,
        )
        self._next_id += 1
        data = frame.SerializeToString()
        if len(data) > self._max_input_size:
            self._resize(len(data))
        try:
            self._producer.send(data)
        except uriel.AgainError:
            logger.debug('no free input buffer; dropping frame %d', frame.id)

    def close(self):
        """Closes the producer, if a frame created it."""
        if self._producer is not None:
            self._producer.close()
            self._producer = None

    def _resize(self, input_size):
        """Creates the producer for inputs of `input_size` bytes, in place of
        one for smaller inputs, e.g. after the resolution changed."""
        if self._producer is not None:
            logger.warning('frames grew to %d bytes; recreating producer %s',
                           input_size, self.uri)
            self.close()
        self._max_input_size = input_size + RAW_FRAME_HEADROOM
        self._producer = uriel.Producer(
            self.uri,
            input_type=RAW_FRAME_TYPE,
            max_input_size=self._max_input_size,
        )
        logger.info('publishing raw frames as %s, up to %d bytes each',
                    self.uri, self._max_input_size)
