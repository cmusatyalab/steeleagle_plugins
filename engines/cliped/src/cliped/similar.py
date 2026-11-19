"""defer_similar: a picked tile that shows what a tile queued or sent
before showed goes to the back of the send queue, behind every new tile, so
the link is spent on new views first; it is still sent once nothing new
waits (unless the full queue drops it first, as it drops the lowest first).
It runs only on the tiles picked to send (at most max_send a frame), not on
every tile of a frame.

Two tiles are the same view when ORB keypoints of one match at least
`min_matches` of the other's under one rotation, scale and shift (RANSAC on
a partial affine transform). That allows for the camera moving between
frames, so the object slides across the tile or jumps a tile over, while
keypoints of plain ground, which match some in any two tiles, are rarely
geometrically consistent. Only tiles whose CLIP embeddings are at least
GATE alike are compared, which skips most pairs for free.

Measured on the tiles the verifier got from the sandr_test flight with the
prompt "signs of human activity" (2026-11-19, 200x200 tiles of 720p frames,
ORB at 200 features): a bike seen in consecutive picked frames (~6 frames
apart) shared 45-139 consistent matches, while tiles more than 60 frames
apart shared a median of 3 (99.9th percentile 25), and one person seen from
two angles 30. At 40, of the bike's 9 tiles in one pass, the first and the
two where it had moved furthest count as new.

On heisenberg (Tachyon, Cortex-A55) a picked tile costs 3.3 ms for its
keypoints and 0.3-0.5 ms per tile compared: in a replay of those tiles a
median 4.5 ms (p95 8.8 ms, max 10.6), and 35 ms at worst, with all MEMORY
tiles passing the gate; scoring a frame takes ~275 ms.
"""
import collections
import logging

import cv2
import numpy as np

logger = logging.getLogger("cliped.similar")

# Keypoints found in each tile
FEATURES = 200
# Lowe's ratio test: a match counts only when clearly better than the
# keypoint's second best
RATIO = 0.8
# Pixels a match may be off the fitted transform and still count
REPROJECTION_PX = 4.0
# Least cosine similarity of the CLIP embeddings of two tiles for them to be
# compared at all. Every pair of near-copies measured was above 0.95; pairs
# under it are not compared
GATE = 0.9
# Tiles remembered to compare against, the most recent
MEMORY = 64
# The default of the defer_similar_matches arg
MIN_MATCHES = 40


class SimilarFilter:
    """Remembers the new tiles and tells the ones too like them."""

    def __init__(self, min_matches=MIN_MATCHES, *, memory=MEMORY, gate=GATE):
        self.min_matches = min_matches
        self.gate = gate
        self._orb = cv2.ORB_create(nfeatures=FEATURES, edgeThreshold=15, patchSize=15)
        self._matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        # (frame id, embedding, keypoint positions, descriptors) of each new tile
        self._seen = collections.deque(maxlen=memory)
        self.similar = 0

    def features(self, rgb):
        """The ORB keypoint positions and descriptors of the RGB tile `rgb`."""
        keypoints, descriptors = self._orb.detectAndCompute(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), None)
        return np.float32([k.pt for k in keypoints]).reshape(-1, 2), descriptors

    def matches(self, a, b):
        """How many keypoints of the features `a` match `b`'s under one
        rotation, scale and shift."""
        (pa, da), (pb, db) = a, b
        if da is None or db is None or len(da) < 4 or len(db) < 4:
            return 0
        good = [m[0] for m in self._matcher.knnMatch(da, db, k=2)
                if len(m) == 2 and m[0].distance < RATIO * m[1].distance]
        if len(good) < 4:
            return 0
        src = pa[[m.queryIdx for m in good]]
        dst = pb[[m.trainIdx for m in good]]
        _, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC,
                                                 ransacReprojThreshold=REPROJECTION_PX)
        return int(inliers.sum()) if inliers is not None else 0

    def is_new(self, rgb, embedding, frame_id):
        """Whether the RGB tile `rgb` of frame `frame_id` is new: False if it
        is too like a new tile before it, which it then names in the log;
        True, remembering it, if not. `embedding` is its unit CLIP embedding,
        or None to compare it with every tile remembered."""
        feats = self.features(rgb)
        v = None if embedding is None else np.asarray(embedding, dtype=np.float32)
        for seen_id, seen_v, seen_p, seen_d in reversed(self._seen):
            if v is not None and seen_v is not None and float(v @ seen_v) < self.gate:
                continue
            n = self.matches(feats, (seen_p, seen_d))
            if n >= self.min_matches:
                self.similar += 1
                logger.info("the tile of frame %d is like the tile of frame %d (%d keypoints match); "
                            "it goes behind the new tiles", frame_id, seen_id, n)
                return False
        self._seen.append((frame_id, v, *feats))
        return True
