import cv2
import numpy as np
from steeleagle_protocol.v1.messages.frame import frame_pb2

RGB_CHANNELS = 3
FRAME_WIDTH = 320
FRAME_HEIGHT = 240
MARKER_SIDE = 60
WHITE = 255


def make_frame(
    markers=(), dictionary="DICT_ARUCO_ORIGINAL", width=FRAME_WIDTH, height=FRAME_HEIGHT
):
    """A white RawFrame with each (id, left, top) of `markers` drawn on it,
    MARKER_SIDE pixels square."""
    gray = np.full((height, width), WHITE, dtype=np.uint8)
    markers_dict = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dictionary))
    for marker_id, left, top in markers:
        image = cv2.aruco.generateImageMarker(markers_dict, marker_id, MARKER_SIDE)
        gray[top : top + MARKER_SIDE, left : left + MARKER_SIDE] = image
    rgb = cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)
    return frame_pb2.RawFrame(
        data=rgb.tobytes(), h_res=width, v_res=height, channels=RGB_CHANNELS, id=1
    )
