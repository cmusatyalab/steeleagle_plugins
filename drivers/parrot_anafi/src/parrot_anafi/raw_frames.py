"""Publishes the drone's decoded video frames to Gabriel, as RawFrame inputs.

eagled tells the driver where to publish them, through two environment
variables: the producer's name, and the flow's. It sets them whenever the
vehicle runs a Gabriel flow, so that a flow which takes raw frames gets them
even if it starts after the driver.
"""
import logging
import os

import cv2
from google.protobuf.timestamp_pb2 import Timestamp

import steeleagle_protocol.v1.messages.telemetry.telemetry_pb2 as telemetry_proto

logger = logging.getLogger('parrot-anafi/raw-frames')

RAW_FRAMES_PRODUCER_ENV = 'GABRIEL_RAW_FRAMES_PRODUCER'
FLOW_ENV = 'GABRIEL_FLOW'
# Engines read a RawFrame's pixels as RGB, one byte per channel.
RGB_CHANNELS = 3
# Room in an input for a RawFrame's fields besides its pixels: its ids,
# timestamp and pose.
RAW_FRAME_HEADROOM = 16 << 10
RAW_FRAME_TYPE = telemetry_proto.RawFrame.DESCRIPTOR.full_name


class RawFramePublisher:
    """Publishes frames as the Gabriel producer `producer_name`, on `flow`.

    `pose` returns the vehicle's PositionInfo and GimbalInfo, which go with
    each frame. The producer is created on the first frame, large enough for
    it, so a publisher must be used, and closed, on one thread.
    """

    def __init__(self, producer_name, flow, pose):
        # Installed only if eagled gave install.sh its wheel, so imported only
        # when publishing.
        import gabriel

        self._gabriel = gabriel
        self._producer_name = producer_name
        self._flow = flow
        self._pose = pose
        self._producer = None
        self._max_input_size = 0
        self._next_id = 0

    def publish(self, bgr_frame):
        """Publishes `bgr_frame`, an image as OpenCV decodes it (BGR). A frame
        that finds no free input buffer is dropped: newer frames follow."""
        rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        timestamp = Timestamp()
        timestamp.GetCurrentTime()
        position_info, gimbal_info = self._pose()
        frame = telemetry_proto.RawFrame(
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
            self._producer.send(self._flow, data)
        except self._gabriel.AgainError:
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
                           input_size, self._producer_name)
            self.close()
        self._max_input_size = input_size + RAW_FRAME_HEADROOM
        self._producer = self._gabriel.Producer(
            self._producer_name,
            input_type=RAW_FRAME_TYPE,
            max_input_size=self._max_input_size,
        )
        logger.info('publishing raw frames as %s on flow %s, up to %d bytes each',
                    self._producer_name, self._flow, self._max_input_size)


def from_environment(pose):
    """Returns a publisher where eagled says to publish raw frames, or None if
    it says nothing (the vehicle runs no Gabriel flow) or Gabriel is not
    installed (eagled had no wheel for install.sh)."""
    producer_name = os.environ.get(RAW_FRAMES_PRODUCER_ENV)
    flow = os.environ.get(FLOW_ENV)
    if not producer_name or not flow:
        return None
    try:
        return RawFramePublisher(producer_name, flow, pose)
    except ImportError:
        logger.warning('Gabriel is not installed, so raw frames are not published; '
                       'start eagled with -gabriel-wheel and reinstall the driver')
        return None
