# cliped

SteelEagle engine plugins for prompt-guided edge filtering:

- `cliped.plugin.toml`, the edge filter: each camera frame (`RawFrame`) is cut into overlapping square tiles, every tile is scored against a text or image prompt with MobileCLIP2-S0, the frame's best tile is sent to the remote verifier, and the tiles worth sending wait in a prioritized send queue until the verifier has them, sent again by the engine's `reintegration` producer.
- `verifier.plugin.toml`, the verifier: asks a vision-language model on Ollama (Qwen3-VL 8B Instruct by default) whether each tile matches the prompt, and forwards it if so, dropping it if not. Its answer goes back to cliped, which learns from it.
- `box_rerank.plugin.toml`, the box reranker: decides which of a local detector's boxes, if any, is the target, by what the verifier said about boxes like it, and sends the boxes it has not heard about to the verifier off the frame path. See [box_rerank](#box_rerank).

eagled picks one with `STEELEAGLE_PLUGIN`; unset runs the edge filter.

A tile's score is `s = v·t − max_j v·b_j`: how much more its embedding `v` looks like the prompt `t` than like the closest background `b_j`. A tile is queued when `s > max(0, thr + margin · std(s))`, where `thr` is an EMA of each frame's `k`-th best score and `std(s)` is taken over the frame's tiles. At most `max_send` tiles of a frame are queued.

Each input is replaced with the best tile cut from it: an RGB `RawFrame` at the tile's native resolution, with the id, timestamp and pose of the frame. If it has none, the input is dropped. The result is a `ClassificationResult` of that tile: `class` is the prompt (the text, or the image's file name) and `score` is `s`. The engine forwards that tile blindly and does not track it. Every tile the input picked (up to `max_send`), the forwarded one included, is queued for the engine's `reintegration` producer.

The `reintegration` producer (`[[engine.producers]]` in `cliped.plugin.toml`) is what delivers tiles to the verifier: the queued tiles, best first, one at a time, each until the verifier has it, whether or not frames come in. A flow names the producer `local.<cliped>.reintegration`; it runs in the engine's process, publishing on `STEELEAGLE_ENGINE_ENDPOINT.reintegration`. Leave the engine a dead end (no edge out of it, so Uriel drops what it forwards), give the producer one token, and route the producer to the verifier.

cliped does not compress tiles. Put an encoder after its producer, e.g. the thumbnail plugin's WebP encoder, and its decoder before the verifier:

```
camera → rescale (720p) → cliped   (a dead end)
cliped.reintegration → webp encoder ══ link ══ webp decoder → verifier
```

`tile_size` and `stride` are tuned for 720p frames, so rescale them first.

With an `image` prompt, `t` is the reference image's embedding by the same image encoder as the tiles (the whole image, squeezed to a square). Image-to-image similarities run well above the image-to-text similarities of the backgrounds, so most tiles score `s > 0` and the adaptive threshold alone decides what is sent.

## Learning from the verifier

With `learn` (on by default), every `RESULT` the verifier sends back for a tile teaches cliped: the result is a `GenericResult` holding `{"match": true|false}`, and cliped keeps the tile's embedding, in its good tiles on a yes and its background tiles on a no. The learned tiles gate `s` rather than join it, the same way for text and image prompts. Tile-to-tile similarities (~0.8) dwarf a tile's similarity to the prompt (~0.5 to a reference photo, ~0.25 to text), so a learned tile in `s` would outscore the prompt everywhere.

- A tile at least `learn_similarity` to a **confirmed** background tile, and closer to it than to any good tile, is not sent.
- A tile at least `good_similarity` to a **confirmed** good tile, and no closer to a background tile, is sent, whatever its `s`.
- Any other tile is decided by `s` as before.

After a change, every queued tile is rescored and reprioritized: each waiting tile that no longer passes is dropped (tiles in flight are kept), and tiles like a good tile go ahead of all the others in the queue, best score first within each group.

A background tile is confirmed once the verifier has said no `confirmations` times to tiles within `learn_similarity` of it. The verifier misses targets too, so with a single no, one missed target would suppress the target from then on. A distractor seen frame after frame (a car, a patch of ground) is soon said no to again. For the same reason, a no is never learned if it looks like a good tile: at least as close to the good tiles as to the background tiles, or within `learn_similarity` of a good tile. A tile is added only if it's below `learn_similarity` (`good_similarity` for a good tile) to everything in its set, and at most `max_learned` per set are kept, oldest first out.

A good tile is confirmed the same way, once the verifier has said yes `good_confirmations` times to tiles within `good_similarity` of it; until then it is a candidate, which still guards tiles like it from being learned or dropped as background but forces nothing. The verifier also says yes to the odd distractor (a rusty tanker, a traffic cone), and since `max_send` is 1, one wrong yes that forced its look-alikes would take the frame's only slot from the target in every frame after. A target seen frame after frame is soon said yes to again. A no within `good_similarity` of a candidate counts against it, and once its no's reach its yes's the candidate is forgotten, so tiles like it can be learned as background. `good_similarity` is above `learn_similarity` because tiles of one scene are ~0.8 similar to each other: a good tile that close would force the plain ground around the target.

Replayed over the logged tiles of the normal-network runs (3 scenes × 3 trials), the defaults cut the tiles sent by 24% with an image prompt (precision 30% → 39%) without losing a tile the verifier confirmed, and by 12% with a text prompt (precision 51% → 57%), losing 10 of 444 confirmed tiles, all but one of them blurry or not a person. A lower `learn_similarity` suppresses more, but below ~0.8 it starts suppressing targets: a small target lying on the ground is about 0.8 similar to an empty tile of that ground.

## The send queue

The engine only adds to the queue; the `reintegration` producer delivers it. The producer has one tile in the flow at a time: it publishes the best waiting tile, and the next only once that one's turn in the flow has ended. With one token, the flow paces it, and the client never has a second input of it waiting to replace the first. Every input ends in the flow with an event, so no timer is needed:

| Event about the tile's latest send | Effect |
| --- | --- |
| the verifier's `RESULT` (yes or no) | the verifier read it: it leaves the queue |
| `SKIPPED` or `TIMED_OUT` at any engine, or a `RESULT` of another engine that dropped it (before the verifier) | its turn ended unread: it waits to be sent again |
| `DISCONNECTED`, or `LOST` (the event that settled it may be among the lost) | it waits to be sent again |
| `ACCEPTED`, `SENT` | still in flight: for a remote verifier, `ACCEPTED` means only that its server got the tile |

When more than `max_queue` tiles wait, the lowest-priority tile is dropped, in flight or not: not a must-send, lowest score. A tile sent again may reach the verifier twice, if its `RESULT` was among the lost events; that is the price of never losing one.

## Args

| Arg | Default | Description |
| --- | --- | --- |
| `prompt` | | What to search for, as a noun phrase, e.g. `person lying down`; this or `image` is required |
| `image` | | An image of what to search for, instead of `prompt`: a file in the daemon's files directory (`STEELEAGLE_FILES`). With both set, `image` is used |
| `verifier` | | Required: the verifier's service name on the remote daemon, qualified with `STEELEAGLE_REMOTE_QUALIFIER` |
| `quantized` | `true` | INT8 image encoder (`models/image_encoder_int8.tflite`); `false` runs the FP32 one (`models/distill_visual.pt`) in PyTorch |
| `tile_size` | `200` | Side of the square tiles, in pixels |
| `stride` | `150` | Distance between neighboring tiles, in pixels |
| `margin` | `0.25` | How far above the threshold a tile must be, in std(s) |
| `max_send` | `1` | Most tiles of one frame queued; the best one replaces the frame |
| `backgrounds` | `["grass", "dirt", "rocks", "trees", "a road", "a roof"]` | Background prompts |
| `k` | `2` | The threshold follows each frame's k-th best score |
| `ema` | `0.9` | Weight of the threshold's old value at each frame |
| `max_queue` | `64` | Most tiles waiting for the verifier |
| `npu` | `true` | With `quantized`, run on the HTP through QNN (`libs/`); `false` runs it on the CPU |
| `threads` | `4` | CPU threads for the text encoder, and for the image encoder off the HTP |
| `learn` | `true` | Learn from the verifier's answers (see above) |
| `learn_similarity` | `0.85` | Cosine similarity of tile embeddings at which two tiles count as the same thing, for the background gate and for adding a background tile |
| `confirmations` | `2` | No's to tiles within `learn_similarity` of each other before tiles like them stop being sent |
| `good_similarity` | `0.9` | Cosine similarity at which a tile counts as the same thing as a good tile, for forcing it to be sent and for adding a good tile |
| `good_confirmations` | `2` | Yes's to tiles within `good_similarity` of each other before tiles like them are sent whatever their score |
| `max_learned` | `32` | Most yes and most no answers kept learned, each |
| `log_scores` | `false` | Log every frame's scores, not only frames that pick a tile |
| `log_tiles` | `false` | Write each input frame to `STEELEAGLE_FILES/<engine>/in/<frame id>.jpeg` and each tile sent to `STEELEAGLE_FILES/<engine>/out/<frame id>_<tile number>.jpeg` (JPEG, quality 75). `<engine>` is `STEELEAGLE_ENGINE_ENDPOINT`, the frame id is the `RawFrame`'s `id`, and the tile number is the tile's index in the frame's tiling, row by row. A tile sent again overwrites its file |

## The verifier

The verifier takes RGB `RawFrame`s (a tile from cliped, decoded) and asks the model, through Ollama's `/api/chat`, whether the frame shows the `prompt`; with an `image`, it shows the model the reference image, shrunk to at most 448 px on its longer side, first and asks whether the frame, seen from above, contains that same target (same appearance, not merely the same category). The model answers yes or no. A yes forwards the frame unchanged; a no, any other answer, or a failure to reach Ollama drops it. Every input the model judged gets a `GenericResult` holding `{"match": true|false}`, so cliped sees a `RESULT` for every tile it sent, from the engine or its producer, and learns from it.

It needs an Ollama server (`ollama serve`); at startup it pulls the model if the server does not have it. It does not load torch or the CLIP models.

| Arg | Default | Description |
| --- | --- | --- |
| `prompt` | | What to look for, as a noun phrase; this or `image` is required |
| `image` | | An image of what to look for, instead of `prompt`: a file in `STEELEAGLE_FILES`. With both set, `image` is used |
| `model` | `qwen3-vl:8b-instruct` | Ollama model to ask |
| `host` | | Ollama server URL; blank uses `OLLAMA_HOST`, or `http://localhost:11434` |
| `timeout_ms` | `30000` | Wait for the model's answer about one frame before dropping it |
| `question` | | The question asked about each frame, instead of the default one; `{prompt}` in it is replaced by `prompt` (or the image's name). With `image`, the model sees the reference first and the frame second. Anything but a yes drops the frame. `box` asks about the object in the red box, for box_rerank's crops: whether it is `prompt`, or, with `image`, the reference's target |
| `log_tiles` | `false` | Write each input to `STEELEAGLE_FILES/<engine>/in/<frame id>.jpeg` and each frame forwarded to `STEELEAGLE_FILES/<engine>/out/<frame id>_0.jpeg` (JPEG, quality 75) |
| `min_side` | `0` | Enlarge each frame so its shorter side is at least this many pixels before the model sees it (box_rerank's small crops); 0 for as is |

Give the edge into the verifier a timeout above the model's time per tile: a tile that times out there is sent again.

## box_rerank

A detector on the vehicle (YOLO on the HTP) confuses look-alikes with the target, e.g. a white folding chair or a dark bag for a robot dog, and reports them at high confidence. box_rerank sits between the detector and the tracker: it gets the same frames as the detector, subscribes to its events, and reports at most one box per frame, the one it has the most reason to believe, or none. A false positive costs a tracker more than a missed frame, so when every candidate is held back the frame has no detection.

```
camera ─► detection_qnn (low confidence)
   └────► box_rerank ─► Track action                (onboard, every frame)
          box_rerank.reintegration ─► webp encoder ══ link ══ webp decoder ─► verifier (question = "box")
```

For each frame, the candidates are the detections of `classes` (by default `class` alone), the best `max_candidates` by confidence. Each is cropped as a square `context` times its longer side and embedded by the CLIP image encoder (~6 ms a crop on the HTP). What the verifier said about earlier crops, learned as the edge filter learns it (clip.Selector; see [Learning from the verifier](#learning-from-the-verifier)), gives each a verdict:

| Verdict | When | Reported |
| --- | --- | --- |
| vetoed | at least `learn_similarity` to a crop the verifier said no to `confirmations` times, and closer to it than to any yes | never |
| confirmed | at least `good_similarity` to a crop the verifier said yes to `good_confirmations` times, and no closer to a no | first, at any confidence |
| unverified | anything else | if its confidence is at least `confidence`, unless `require_verified` |

Of the eligible candidates, a confirmed one wins. Then, with `rank = "prompt"` (the default), the one with the highest CLIP prompt score `s`: the frame's candidates are ranked against each other, as cliped ranks a frame's tiles, so `s`'s scale (higher against an image than a text prompt) doesn't matter. With `rank = "confidence"`, the most confident (plus `prompt_weight` times `s`, 0 by default): only for a prompt that just names the detector's class, which can't tell its boxes apart. Replayed on the robot dog runs (prompt "a quadruped robot dog", against a bag and a chair), ranking by `s` cut the false positives from 17-34 to 0-3 per profile. The box is reported as is, renamed `class`, as the frame's `DetectionResult` result; the frame is dropped, as `cop_tracking` drops it, so box_rerank replaces it as the tracker's engine.

Learning never holds up a frame. Each unverified candidate is queued for the verifier unless it is at least `novelty` similar to a crop queued, or to one the verifier answered in the last `cooldown` seconds: a chair in every frame is asked about once, then again after the cooldown, and its second no vetoes it. The box reported goes first. The crop sent is a square `verify_context` times the box's longer side (at least `min_verify_side`, scaled down to `max_verify_side`), with the box drawn in red: the context helps the model recognize a small object, and the box says which object is meant when the target stands next to a distractor. The reintegration producer delivers the queue as the edge filter's does ([The send queue](#the-send-queue)); a no or a yes that confirms drops the queued crops it settles.

Run the detector with a lower `confidence` (and `nms_score_threshold`) than usual, e.g. 0.25, so a target it is unsure of is still a candidate; box_rerank's own `confidence` decides which unverified boxes are reported. On the remote side, run the verifier with `question = "box"` and `min_side` around 224, so small crops cross the link small and are enlarged for the model.

To track one individual among others of its class (one person of several, say the one in the blue jacket), give the verifier a reference photo: set its `image` along with `question = "box"`. It then asks whether the object in the red box is the target in the reference, judging only that object, so a crop whose context also holds the target is still a no for the person next to it. Give box_rerank the same `image`: ranking by `s` (the default `rank`), until the verifier has answered, the individuals in a frame are then ranked against each other by their CLIP likeness to the reference, onboard, rather than by confidence, and its answers take over as they come. Frames never wait for the verifier either way. `require_verified = true` reports no box at all until the verifier has confirmed the target (and none while it looks unlike every confirmed crop): only for when reporting the wrong individual is worse than reporting none. Individuals of one class may embed close together: if a no about another one vetoes the target, raise `learn_similarity`.

Ranking orders a frame's boxes against each other, so it always reports one, even when the target isn't among them: then it reports the box most like it, e.g. another person. With `gate = true`, each crop also gets a margin, how much more it is like the prompt ("a person in a red shirt") than like `class_prompt` ("a person"), and box_rerank learns where to cut it from the verifier's answers: the midpoint between the mean margin of the crops it said yes to and of those it said no to (once it has one of each, and while the yes's are higher). An unverified box is reported only above the cut; a confirmed one regardless. This is apart from the tile learning, which compares crops with each other and can't tell instances of one class apart; the margin measures the attributes the prompt names. Offline, on two drone videos of three people, it raised precision for a person in a red shirt from 0.69 to 0.91 at the same recall. The margin is logged as `margin` in `log_candidates`, and the cut in the summary.

`log_candidates` writes every candidate of every frame (its box, confidence, `s`, verdict, whether it was reported and queued), to score what was reported against labeled boxes; `log_tiles` writes each crop the verifier is sent.

| Arg | Default | Description |
| --- | --- | --- |
| `detector` | | Required: the detection engine on this daemon, qualified with `STEELEAGLE_LOCAL_QUALIFIER` |
| `verifier` | | Required: the verifier on the remote daemon, qualified with `STEELEAGLE_REMOTE_QUALIFIER` |
| `class` | | Required: the class reported, as the tracker asks for it |
| `classes` | `[]` | The detector's classes whose boxes are candidates; empty for `class` alone |
| `prompt` / `image` | | The target, for the CLIP prompt score `s`; one is required |
| `confidence` | `0.5` | Least confidence for an unverified box to be reported |
| `require_verified` | `false` | Report only confirmed boxes |
| `max_candidates` | `8` | Most boxes of a frame embedded |
| `context` | `1.25` | Side of the crop embedded, times the box's longer side |
| `verify_context` | `2.0` | Side of the crop the verifier sees, times the box's longer side |
| `min_verify_side` / `max_verify_side` | `96` / `256` | Bounds of that crop's side, in pixels |
| `novelty` | `0.92` | Similarity at which a crop counts as one queued or recently answered |
| `cooldown` | `5.0` | Seconds before crops like an answered one are asked about again |
| `prompt_weight` | `0.0` | Weight of `s` against confidence among unverified boxes, with `rank = "confidence"` |
| `rank` | `prompt` | How a frame's unverified boxes are ranked: `prompt` (by `s`, against each other), or `confidence` |
| `gate` | `false` | Learn from the verifier whether the best box is the target at all, and report none if not (see below; text prompts only) |
| `class_prompt` | `a <first class>` | With `gate`: the prompt for any box of the class |
| `timeout` | `1000` | Longest wait for the detector's result on a frame, in ms |
| `learn_similarity`, `confirmations`, `good_similarity`, `good_confirmations`, `max_learned` | `0.85`, `2`, `0.9`, `2`, `32` | As for the edge filter |
| `backgrounds`, `quantized`, `npu`, `threads`, `max_queue` | | As for the edge filter |
| `log_tiles` | `false` | Write each crop sent to `STEELEAGLE_FILES/<engine>/out/` |
| `log_candidates` | | CSV of every candidate of every frame |

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
