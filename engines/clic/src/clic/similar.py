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

logger = logging.getLogger("clic.similar")

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

    def forget(self):
        """Forgets every tile seen: after a prompt switch, a tile like one
        sent for the old prompt is new to the new one."""
        self._seen.clear()

    def features(self, rgb):
        """The ORB keypoint positions and descriptors of the RGB tile `rgb`."""
        keypoints, descriptors = self._orb.detectAndCompute(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), None)
        return np.float32([k.pt for k in keypoints]).reshape(-1, 2), descriptors

    def keypoints(self, features):
        """How many keypoints the features hold."""
        return 0 if features is None or features[1] is None else len(features[1])

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

    def same(self, a, b, va=None, vb=None):
        """How many keypoints of the features `a` and `b` match (matches), or
        0 without comparing if their unit CLIP embeddings `va` and `vb`, when
        both given, are under the gate."""
        if va is not None and vb is not None and float(np.asarray(va) @ np.asarray(vb)) < self.gate:
            return 0
        return self.matches(a, b)

    def is_new(self, rgb, embedding, frame_id, feats=None):
        """Whether the RGB tile `rgb` of frame `frame_id` is new: False if it
        is too like a new tile before it, which it then names in the log;
        True, remembering it, if not. `embedding` is its unit CLIP embedding,
        or None to compare it with every tile remembered; `feats`, its
        features if already computed."""
        feats = self.features(rgb) if feats is None else feats
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


# The defaults of clic_smart_search's repeat check (its repeat_* args)
REPEAT_MATCHES = 10
REPEAT_SIDE = 96
REPEAT_GATE = 0.8
REPEAT_MEMORY = 64


class SentMemory:
    """clic_smart_search's repeat check: remembers what was sent to the
    verifier, one entry per object, for `window_s` (None: the whole flight),
    whatever the answer, so the same thing is not sent twice unless its
    score jumps.

    A crop is the object of an entry when at least `min_matches` ORB
    keypoints of the two match under one rotation, scale and shift (as
    SimilarFilter), after both were scaled up to at least `side` px: a 48 px
    crop has too little inside ORB's border to find keypoints at all. Only
    entries whose CLIP embeddings are at least `gate` alike are compared.
    An entry follows its object: a crop of it held back, or sent on a jump,
    becomes its view, so the object can drift as the drone moves.

    Replayed on run 2.3.14's sends (NOMAD, MOBDrone, both field searches),
    10 keypoints at 96 px over 30 s held back 72 of the 238 false sends and
    57 repeat sends of targets, and every occurrence was still sent; over
    the whole flight 75, losing one of nomad2's 28 occurrences."""

    def __init__(self, min_matches=REPEAT_MATCHES, *, window_s=None, side=REPEAT_SIDE, gate=REPEAT_GATE,
                 memory=REPEAT_MEMORY):
        self.min_matches = min_matches
        self.window_s = window_s
        self.side = side
        self.gate = gate
        self.memory = memory
        self._orb = cv2.ORB_create(nfeatures=FEATURES, edgeThreshold=15, patchSize=15)
        self._filter = SimilarFilter(min_matches)
        # Each entry: [embedding, features, score of the last send, time last seen, frame id last sent]
        self._entries = []

    def forget(self):
        """Forgets everything sent: after a prompt switch, a crop like one
        sent for the old prompt is new to the new one."""
        self._entries.clear()

    def features(self, rgb):
        """The ORB keypoint positions and descriptors of the RGB crop `rgb`,
        scaled up to `side` px if smaller."""
        if min(rgb.shape[:2]) < self.side:
            rgb = cv2.resize(rgb, (self.side, self.side), interpolation=cv2.INTER_CUBIC)
        keypoints, descriptors = self._orb.detectAndCompute(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), None)
        return np.float32([k.pt for k in keypoints]).reshape(-1, 2), descriptors

    def like(self, embedding, feats, now):
        """The entry the crop with unit CLIP embedding `embedding` and
        features `feats` is a repeat of (the most recently seen one it
        matches), or None. Entries older than the window are dropped."""
        if self.window_s is not None:
            self._entries = [e for e in self._entries if now - e[3] <= self.window_s]
        if feats is None or feats[1] is None:
            return None
        x = np.asarray(embedding, dtype=np.float32)
        for e in sorted(self._entries, key=lambda e: e[3], reverse=True):
            if float(x @ e[0]) >= self.gate and self._filter.matches(feats, e[1]) >= self.min_matches:
                return e
        return None

    def held_back(self, entry, embedding, feats, now):
        """A crop of `entry`'s object was held back: it becomes the entry's view."""
        entry[0], entry[1], entry[3] = np.asarray(embedding, dtype=np.float32), feats, now

    def sent(self, entry, embedding, feats, score, now, frame_id):
        """The crop was sent: it becomes `entry`'s view and score, or (entry
        None) a new entry, the oldest dropped past `memory`."""
        if entry is not None:
            entry[:] = [np.asarray(embedding, dtype=np.float32), feats, score, now, frame_id]
            return
        self._entries.append([np.asarray(embedding, dtype=np.float32), feats, score, now, frame_id])
        if len(self._entries) > self.memory:
            self._entries.pop(min(range(len(self._entries)), key=lambda i: self._entries[i][3]))
