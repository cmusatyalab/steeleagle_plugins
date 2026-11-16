# Detection Crop

SteelEagle engine plugin that crops each camera frame (`RawFrame`) to the highest-scoring detection of a class that a detection engine on the same daemon found in it, and forwards the crop in place of the frame. The crop keeps the frame's id, timestamp and pose, and replaces its input, which keeps the input's id. The result is a `ClassificationResult` of the crop: `class` is the detection's class, as the detector names it, and `score` is its confidence.

The engine forwards each crop blindly and does not track it. It also queues each crop in a prioritized send queue, and the engine's `reintegration` producer is what delivers them to a remote verifier: most confident first, one at a time, each until the verifier has it.

The engine subscribes to the detector's events, so the flow sends frames to both engines. Leave the crop engine a dead end (no edge out of it, so Uriel drops what it forwards), and route its producer to the verifier:

```
camera ──► detection
   └─────► detection-crop   (a dead end)
           detection-crop.reintegration ──► (encoder ══ link ══ decoder) ──► verifier
```

detection-crop does not compress crops. To compress them, put an encoder after its producer, e.g. the thumbnail plugin's WebP encoder, and its decoder before the verifier.

Each frame waits for the detector's result on it. A frame is dropped, with no result, if:

- the detector skipped it, timed out, or is disconnected;
- its result names no detection of the class (a detector that drops a frame with no result, as detection-qnn does with `early_discard`, found nothing);
- no result comes within `timeout`;
- the crop and its result are larger than `max_width` x `max_height` allows.

The box is normalized, grown to whole pixels and clipped to the frame. The engine holds one frame at a time, so later frames wait at the server while it waits on the detector.

The detection engine can be the verifier, checking each crop with RF-DETR: see "As a verifier after detection-crop" in its README.

## The reintegration producer

An engine can only send in answer to an input, so a crop that was lost, timed out, or made while the link was down could never reach the verifier unless a later frame carried it. The `reintegration` producer (`[[engine.producers]]` in `detection-crop.plugin.toml`) delivers the queued crops instead, whether or not frames come in. A flow names it `local.<detection-crop>.reintegration`. It runs in the engine's process and publishes on `STEELEAGLE_ENGINE_ENDPOINT.reintegration`. Give it one token.

The producer has one crop in the flow at a time: it publishes the best waiting crop, and the next only once that one's turn in the flow has ended. With one token, the flow paces it, and the client never has a second input of it waiting to replace the first.

## The send queue

Every input ends in the flow with an event, so no timer is needed:

| Event about the crop's latest send | Effect |
| --- | --- |
| the verifier's `RESULT` | the verifier read it: it leaves the queue |
| `SKIPPED` or `TIMED_OUT` at any engine, or a `RESULT` of another engine that dropped it (before the verifier) | its turn ended unread: it waits to be sent again |
| `DISCONNECTED`, or `LOST` (the event that settled it may be among the lost) | it waits to be sent again |
| `ACCEPTED`, `SENT` | still in flight: for a remote verifier, `ACCEPTED` means only that its server got the crop |

When more than `max_queue` crops wait, the crop of the least confident detection is dropped, in flight or not. A crop sent again may reach the verifier twice, if its `RESULT` was among the lost events; that is the price of never losing one.

## Args

| Arg | Default | Description |
| --- | --- | --- |
| `detector` | | Required: service name of the detection engine on this daemon, qualified with `STEELEAGLE_LOCAL_QUALIFIER` |
| `class` | | Required: class to crop to, as the detector names it, ignoring case |
| `verifier` | | Required: service name of the engine on the remote daemon that the crops go to, qualified with `STEELEAGLE_REMOTE_QUALIFIER` |
| `timeout` | `2000` | Longest to wait for the detector's result on a frame, in milliseconds |
| `max_width` | `1920` | Widest crop sent, in pixels |
| `max_height` | `1080` | Tallest crop sent, in pixels |
| `max_queue` | `64` | Most crops waiting for the verifier |

## Development

```
pixi run -e test test   # all tests, including ones through a real Uriel remote crossing
```
