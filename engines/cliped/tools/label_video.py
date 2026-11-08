#!/usr/bin/env python3
"""Label a video frame by frame as TP or TN, producing labels.txt.

    uv run label_video.py video.mp4 [--labels labels.txt]

Labels work like keyframes: every frame takes the label of the nearest
keyframe at or before it, and frames before the first keyframe are TN. So a TP
keyframe labels everything after it TP until a TN keyframe, or the end of the
video if there is none. Re-running on an existing labels file resumes from it,
with a keyframe wherever the label changes. The file has one
"<frame_id> <TP|TN>" per line. Frame IDs are sequential decode indices, the
same numbering tools/evaluate.py uses.

Keys:
    d / right    next frame            a / left    previous frame
    l            forward 30 frames     j           back 30 frames
    space        add a keyframe that flips the label from this frame on,
                 or remove the keyframe already on this frame
    r            start/finish a range, marking it TP (frames after it keep their label)
    t            start/finish a range, marking it TN (frames after it keep their label)
    esc          cancel a pending range
    p            play / pause
    s            save                  q           save and quit
"""
import argparse
from pathlib import Path

import cv2
import numpy as np

WINDOW = "label_video"
JUMP = 30
KEY_LEFT, KEY_RIGHT = 0xFF51, 0xFF53
KEY_ESC = 27
GREEN, GREY = (0, 200, 0), (90, 90, 90)


def load_labels(path):
    """Read a labels file into {frame_id: "TP" | "TN"}."""
    labels = {}
    for lineno, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit() or parts[1] not in ("TP", "TN"):
            raise ValueError(f"{path}:{lineno}: expected '<frame_id> <TP|TN>', got {line!r}")
        labels[int(parts[0])] = parts[1]
    return labels


def save_labels(path, n_frames, tps):
    lines = (f"{i} {'TP' if i in tps else 'TN'}\n" for i in range(n_frames))
    Path(path).write_text("".join(lines))


class Frames:
    """Random access to a video, decoding strictly in order.

    Seeking with CAP_PROP_POS_FRAMES can land on the wrong frame, which would
    silently misalign the labels, so frames are decoded sequentially and kept
    in memory as downscaled JPEGs.
    """

    def __init__(self, path, max_width):
        self.path = str(path)
        self.max_width = max_width
        self.cap = cv2.VideoCapture(self.path)
        if not self.cap.isOpened():
            raise SystemExit(f"could not open video: {path}")
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30
        self.estimate = max(int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)), 1)
        self.cache = []
        self.exhausted = False

    def _decode_next(self):
        ok, frame = self.cap.read()
        if not ok:
            self.exhausted = True
            return
        h, w = frame.shape[:2]
        if w > self.max_width:
            frame = cv2.resize(frame, (self.max_width, h * self.max_width // w), interpolation=cv2.INTER_AREA)
        self.cache.append(cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])[1])

    def get(self, i):
        """Return (index, image), clamping i to the last frame in the video."""
        while len(self.cache) <= i and not self.exhausted:
            self._decode_next()
        if not self.cache:
            raise SystemExit(f"no decodable frames in {self.path}")
        i = min(i, len(self.cache) - 1)
        return i, cv2.imdecode(self.cache[i], cv2.IMREAD_COLOR)

    def known_count(self):
        """Best current frame count, for display."""
        return len(self.cache) if self.exhausted else max(self.estimate, len(self.cache))

    def exact_count(self):
        """True frame count. The container's own count can be wrong."""
        if self.exhausted:
            return len(self.cache)
        cap = cv2.VideoCapture(self.path)
        n = 0
        while cap.grab():
            n += 1
        cap.release()
        return n


class Session:
    """Labeling state and key handling, kept separate from the window."""

    def __init__(self, tps=()):
        tps = set(tps)
        boundaries = sorted(tps | {i + 1 for i in tps})
        self.keyframes = {i: i in tps for i in boundaries if (i in tps) != (i - 1 in tps)}
        self.pos = 0
        self.playing = False
        self.range_start = None
        self.range_value = None  # True marks TP, False marks TN

    def is_tp(self, i):
        earlier = [k for k in self.keyframes if k <= i]
        return self.keyframes[max(earlier)] if earlier else False

    def tp_spans(self, n_frames):
        """Yield (start, stop) for each run of TP frames, stop exclusive."""
        keys = sorted(k for k in self.keyframes if k < n_frames)
        for start, stop in zip(keys, keys[1:] + [n_frames]):
            if self.keyframes[start]:
                yield start, stop

    def tp_frames(self, n_frames):
        return {i for start, stop in self.tp_spans(n_frames) for i in range(start, stop)}

    def _toggle_keyframe(self):
        if self.pos in self.keyframes:
            del self.keyframes[self.pos]
        else:
            self.keyframes[self.pos] = not self.is_tp(self.pos)

    def _mark_range(self, first, last, value):
        """Set first..last to value, leaving the labels after last unchanged."""
        after = self.is_tp(last + 1)
        self.keyframes = {k: v for k, v in self.keyframes.items() if not first <= k <= last + 1}
        self.keyframes[first] = value
        if after != value:
            self.keyframes[last + 1] = after

    def _range_key(self, value):
        if self.range_start is None or self.range_value != value:
            self.range_start, self.range_value = self.pos, value
            return
        self._mark_range(min(self.range_start, self.pos), max(self.range_start, self.pos), value)
        self.range_start = None

    def handle(self, key):
        """Apply a key press. Returns "save", "quit" or None."""
        code = key & 0xFFFF
        char = chr(code).lower() if code < 256 else ""
        if code == KEY_RIGHT or char == "d":
            self.pos += 1
        elif code == KEY_LEFT or char == "a":
            self.pos = max(self.pos - 1, 0)
        elif char == "l":
            self.pos += JUMP
        elif char == "j":
            self.pos = max(self.pos - JUMP, 0)
        elif char == " ":
            self._toggle_keyframe()
        elif char == "r":
            self._range_key(True)
        elif char == "t":
            self._range_key(False)
        elif code == KEY_ESC:
            self.range_start = None
        elif char == "p":
            self.playing = not self.playing
        elif char == "s":
            return "save"
        elif char == "q":
            return "quit"
        return None

    def render(self, image, n_frames):
        h, w = image.shape[:2]
        is_tp = self.is_tp(self.pos)
        spans = list(self.tp_spans(n_frames))
        cv2.rectangle(image, (0, 0), (w - 1, h - 1), GREEN if is_tp else GREY, 8)

        n_tps = sum(stop - start for start, stop in spans)
        status = f"frame {self.pos}/{n_frames - 1}  {'TP' if is_tp else 'TN'}  ({n_tps} TPs)"
        if self.pos in self.keyframes:
            status += "  keyframe"
        if self.range_start is not None:
            status += f"  range {'TP' if self.range_value else 'TN'} from {self.range_start}"
        cv2.putText(image, status, (16, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(image, status, (16, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                    GREEN if is_tp else (255, 255, 255), 2, cv2.LINE_AA)

        # Timeline strip: TP frames in green, current position in white.
        strip = np.full((14, w, 3), 40, np.uint8)
        to_x = lambda i: min(i * (w - 1) // max(n_frames - 1, 1), w - 1)
        for start, stop in spans:
            strip[:, to_x(start):to_x(stop - 1) + 1] = GREEN
        strip[:, to_x(self.pos)] = (255, 255, 255)
        return np.vstack([image, strip])


def main():
    parser = argparse.ArgumentParser(description="Label video frames as TP or TN.")
    parser.add_argument("video", type=Path)
    parser.add_argument("--labels", type=Path, default=Path("labels.txt"))
    parser.add_argument("--max-width", type=int, default=1280, help="display width in pixels")
    args = parser.parse_args()

    frames = Frames(args.video, args.max_width)
    tps = ()
    if args.labels.exists():
        tps = [i for i, label in load_labels(args.labels).items() if label == "TP"]
        print(f"resuming from {args.labels}: {len(tps)} TPs")
    session = Session(tps)

    def save():
        n = frames.exact_count()
        tps = session.tp_frames(n)
        save_labels(args.labels, n, tps)
        print(f"saved {args.labels}: {n} frames, {len(tps)} TPs")

    def on_trackbar(value):
        session.pos = value

    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    cv2.createTrackbar("frame", WINDOW, 0, max(frames.estimate - 1, 1), on_trackbar)
    play_delay = max(int(1000 / frames.fps), 1)

    try:
        while True:
            session.pos, image = frames.get(session.pos)
            cv2.imshow(WINDOW, session.render(image, frames.known_count()))
            cv2.setTrackbarMax("frame", WINDOW, max(frames.known_count() - 1, 1))
            if cv2.getTrackbarPos("frame", WINDOW) != session.pos:
                cv2.setTrackbarPos("frame", WINDOW, session.pos)

            key = cv2.waitKeyEx(play_delay if session.playing else 30)
            if key != -1:
                action = session.handle(key)
                if action == "save":
                    save()
                elif action == "quit":
                    break
            if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                break
            if session.playing:
                session.pos += 1
                if frames.exhausted and session.pos >= len(frames.cache):
                    session.playing = False
    finally:
        # Also runs on Ctrl-C or a closed window, so labeling work is not lost.
        save()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
