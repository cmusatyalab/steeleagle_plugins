"""Optical-flow keyframe selection.

A frame is the next keyframe when the features of the last keyframe, tracked
into it with Lucas-Kanade optical flow, moved more than min_disparity pixels
on average.
"""

import cv2
import numpy as np

# The server's default (swiftmap.constants.DEFAULT_MIN_DISPARITY), in pixels.
DEFAULT_MIN_DISPARITY = 40.0

# Feature detection on each keyframe.
MAX_CORNERS = 1000
QUALITY_LEVEL = 0.01
MIN_DISTANCE = 8
BLOCK_SIZE = 7
# Pyramidal Lucas-Kanade optical flow.
WIN_SIZE = (21, 21)
MAX_LEVEL = 3
CRITERIA = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01)
# The fewest features a keyframe must have, and must still be tracked into a
# frame for its disparity to count.
MIN_TRACKING_POINTS = 10


class FrameTracker:
    """Decides, frame by frame of one camera, which frames are keyframes."""

    def __init__(self, min_disparity=DEFAULT_MIN_DISPARITY):
        self.min_disparity = min_disparity
        self.kf_gray = None
        self.kf_pts = None
        # Mean disparity from the last keyframe to the last frame compared
        # with it, or None if that frame was not compared (it became the
        # keyframe without one).
        self.last_disparity = None

    def _set_keyframe(self, gray):
        """Makes `gray` the keyframe. Returns whether it has enough features
        to track; if not, the next frame replaces it."""
        self.kf_gray = gray
        self.kf_pts = cv2.goodFeaturesToTrack(
            gray,
            maxCorners=MAX_CORNERS,
            qualityLevel=QUALITY_LEVEL,
            minDistance=MIN_DISTANCE,
            blockSize=BLOCK_SIZE,
        )
        return self.kf_pts is not None and len(self.kf_pts) >= MIN_TRACKING_POINTS

    def is_keyframe(self, gray):
        """Whether the 8-bit grayscale frame `gray` is the next keyframe. If
        it is, it is the keyframe the frames after it are compared with.
        Without min_disparity, every frame is."""
        self.last_disparity = None
        if self.min_disparity <= 0:
            return True
        if (
            self.kf_pts is None
            or len(self.kf_pts) < MIN_TRACKING_POINTS
            # The server would fail on every frame after a change of
            # resolution; here the first frame at the new one starts over.
            or gray.shape != self.kf_gray.shape
        ):
            return self._set_keyframe(gray)
        next_pts, status, _err = cv2.calcOpticalFlowPyrLK(
            self.kf_gray, gray, self.kf_pts, None,
            winSize=WIN_SIZE, maxLevel=MAX_LEVEL, criteria=CRITERIA,
        )
        tracked = status.ravel() == 1
        if np.count_nonzero(tracked) < MIN_TRACKING_POINTS:
            # Lost track of the keyframe: this frame is the next one
            return self._set_keyframe(gray)
        # As the server computes it: mean |dx| and |dy| together, so a 60 px
        # sideways move counts 30
        self.last_disparity = float(np.mean(np.abs(next_pts[tracked] - self.kf_pts[tracked])))
        if self.last_disparity > self.min_disparity:
            return self._set_keyframe(gray)
        return False
