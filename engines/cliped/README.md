# cliped

SteelEagle engine plugin for text-guided edge filtering. Each camera frame (`RawFrame`) is cut into overlapping square tiles, every tile is scored against a text prompt with MobileCLIP2-S0, and the tiles worth sending wait in a prioritized send queue until the remote verifier has them.

A tile's score is `s = v·t − max_j v·b_j`: how much more its embedding `v` looks like the prompt `t` than like the closest background `b_j`. A tile is queued when `s > max(0, thr + margin · std(s))`, where `thr` is an EMA of each frame's `k`-th best score and `std(s)` is taken over the frame's tiles. At most `max_send` tiles of a frame are queued.

Each input is replaced with the best queued tile that is not in flight: an RGB `RawFrame` at the tile's native resolution, with the id, timestamp and pose of the frame it was cut from. If no tile waits, the input is dropped. The result is a `DetectionResult` of the tiles the input picked (class = the prompt, score = `s`, box in the frame), which may not be the tile it carries.

cliped does not compress tiles. Put an encoder after it, e.g. the thumbnail plugin's WebP encoder, and its decoder before the verifier:

```
camera → rescale (720p) → cliped → webp encoder ══ link ══ webp decoder → verifier
```

`tile_size` and `stride` are tuned for 720p frames, so rescale them first.

## The send queue

A tile leaves the queue only on the verifier's `RESULT` event for an input it went out as: the verifier finished it. `ACCEPTED` is not enough: for a remote verifier it means only that the remote server got the tile, and a busy or stuck verifier then produces `ACCEPTED`, `SENT`, `TIMED_OUT` without ever taking it. Until its `RESULT`:

| Event or condition | Effect |
| --- | --- |
| `SKIPPED` / `TIMED_OUT` for the tile's latest carrier | the tile waits to be sent again |
| `DISCONNECTED`, or `LOST` (its `RESULT` may be among the lost) | every tile in flight waits to be sent again |
| `resend_after_ms` without word, e.g. dropped by the encoder before reaching the verifier | the tile waits to be sent again |
| more than `max_queue` tiles | the lowest-scored tile is dropped, in flight or not |

A tile sent again may reach the verifier twice; that is the price of never losing one.

## Args

| Arg | Default | Description |
| --- | --- | --- |
| `prompt` | | Required: what to search for, as a noun phrase, e.g. `person lying down` |
| `verifier` | | Required: the verifier's service name on the remote daemon, qualified with `STEELEAGLE_REMOTE_QUALIFIER` |
| `quantized` | `true` | INT8 image encoder (`models/image_encoder_int8.tflite`); `false` runs the FP32 one (`models/distill_visual.pt`) in PyTorch |
| `tile_size` | `200` | Side of the square tiles, in pixels |
| `stride` | `150` | Distance between neighboring tiles, in pixels |
| `margin` | `0.25` | How far above the threshold a tile must be, in std(s) |
| `max_send` | `1` | Most tiles of one frame queued |
| `backgrounds` | `["grass", "dirt", "rocks", "trees", "a road", "a roof"]` | Background prompts |
| `k` | `2` | The threshold follows each frame's k-th best score |
| `ema` | `0.9` | Weight of the threshold's old value at each frame |
| `max_queue` | `64` | Most tiles waiting for the verifier |
| `resend_after_ms` | `5000` | Wait for a sent tile's `RESULT` before sending it again; keep it above the verifier's edge timeout plus its processing time |
| `npu` | `true` | With `quantized`, run on the HTP through QNN (`libs/`); `false` runs it on the CPU |
| `threads` | `4` | CPU threads for the text encoder, and for the image encoder off the HTP |
| `log_tiles` | `false` | Log every frame's scores, not only frames that pick a tile |

## On the Tachyon

With `quantized` and `npu` (the defaults), the image encoder runs on the HTP through the QNN TFLite delegate and the libraries in `libs/` (QAIRT 2.39). QNN loads them with `dlopen`, so the engine restarts itself with `libs/` on `LD_LIBRARY_PATH`, and puts it on `ADSP_LIBRARY_PATH` too. The text encoder runs FP32 on the CPU, once at startup; it downloads MobileCLIP2-S0 from Hugging Face the first time, into `HF_HOME`.

On a Tachyon (fermi), at 1280x720 with the default tiling (45 tiles a frame):

| | ms/tile | ms/frame |
| --- | --- | --- |
| INT8 on the HTP | 6.1 | 280 (p90 300) |
| INT8 on the CPU | 77 | |

Streaming `street.mp4` unpaced, it processed about 3.5 frames/s, Uriel skipping the frames it was too busy for. 174 of 334 processed frames sent a tile, each confirmed by the verifier's `RESULT` once, with no resends.

## Models

| File | What |
| --- | --- |
| `models/distill_visual.pt` | The distilled FP32 image encoder: a state dict of the reparameterized MobileCLIP2-S0's `visual` |
| `models/image_encoder_int8.tflite` | The INT8 image encoder `tools/quantize_clip.py` built from it: 256x256 RGB int8 in (pixel − 128), unnormalized 512-d int8 embedding out |

The INT8 model's Gather ops (from the export's depthwise rewrite) have int32 indices, which the HTP runs; with the int64 indices the export leaves, the delegate ran them on the CPU, and the model took 19.7 ms/tile instead of 6.1, with identical outputs. `quantize_clip.py` patches every model it converts.

## Tools

`tools/` holds the code that built the models and evaluates them offline, run in the `tools` environment (TensorFlow 2.19 and onnx2tf, without the engine's protocol packages):

| File | What |
| --- | --- |
| `quantize_clip.py` | `calibrate` (tiles from the evaluation videos), `export` (PyTorch → ONNX, with the depthwise rewrite), `convert` (→ full-integer TFLite, percentile-calibrated), `patch` (int32 Gather indices), `verify` (INT8 vs FP32 cosine on held-out tiles) |
| `evaluate.py` | Offline evaluation on a labeled video, with the engine's tiling, encoders and send decision and an oracle verifier |
| `label_video.py` | Labels a video frame by frame as TP/TN; needs an OpenCV with GUI support |
| `make_tiles_dev.py` | Training tiles for distillation, from footage outside the evaluation set |
| `baseline.sh`, `trial.sh`, `run_int8.sh` | Build a model (of the original weights, or of given weights) and evaluate it on the three videos on the NPU |
| `labels/` | Labels of the three evaluation videos |

```
pixi run -e tools quantize --weights models/distill_visual.pt          # build an INT8 model in tools/model
pixi run -e tools quantize --steps patch --model some_model.tflite      # patch an existing model
LD_LIBRARY_PATH=$PWD/libs pixi run -e tools evaluate /home/particle/street.mp4 "person lying down" \
    --npu models/image_encoder_int8.tflite --labels tools/labels/labels_street.txt
```

The videos (`street.mp4`, `traintrack.mp4`, `parking_lot.mp4`) are not in the repository; the tools look for them in `/home/particle` (`--video-dir`, `VIDEO_DIR`). The distillation script that produced `distill_visual.pt` is not here yet.

## Development

```
pixi run -e test test   # all tests, including ones through a real Uriel remote crossing
```
