"""Training tiles for distillation, from footage outside the evaluation set, cut as cliped cuts them, saved as JPEG.

Streams frames, keeping a few random tiles from each, so memory stays small.
"""
import numpy as np, cv2, pickle
from cliped.clip import tile_boxes
from evaluate import read_frames
rng = np.random.default_rng(1); out = []
for video, fps, per_frame in [("search_trial_924.MP4", 0.5, 2), ("gravel_parkinglot.MP4", 1.0, 5), ("gravel_2_grass.mp4", 1.0, 5)]:
    n = 0
    for _, frame in read_frames("/home/particle/" + video, fps, None, 720):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB); boxes = tile_boxes(rgb.shape[1], rgb.shape[0], 200, 150)
        for i in rng.choice(len(boxes), per_frame, replace=False):
            x0, y0, x1, y1 = boxes[i]
            tile = cv2.resize(rgb[y0:y1, x0:x1], (256, 256), interpolation=cv2.INTER_AREA)
            out.append(cv2.imencode(".jpg", cv2.cvtColor(tile, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes())
        n += 1
    print(video, n, "frames", len(out), "tiles so far", flush=True)
pickle.dump(out, open("train_tiles.pkl", "wb")); print(len(out), sum(map(len, out)) / 1e6, "MB")
