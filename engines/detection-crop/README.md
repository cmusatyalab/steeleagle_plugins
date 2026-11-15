# Detection Crop

SteelEagle engine plugin that crops each camera frame (`RawFrame`) to the highest-scoring detection of a class that a detection engine on the same daemon found in it, and sends the crop on to a remote verifier in place of the frame. The crop keeps the frame's id, timestamp and pose, and replaces its input, which keeps the input's id. The result is a `ClassificationResult` of the crop: `class` is the detection's class, as the detector names it, and `score` is its confidence.

Each crop sent waits in a prioritized send queue until the verifier has it, and the engine's `reintegration` producer sends the waiting crops again, most confident first.

The engine subscribes to the detector's events, so the flow sends frames to both engines. The crop engine's outgoing edges, and its producer's, carry the crops to the verifier:

```
camera ──► detection
   └─────► detection-crop ───────────────┐
           detection-crop.reintegration ─┴─► (encoder ══ link ══ decoder) ─► verifier
```

detection-crop does not compress crops. To compress them, put an encoder after it and after its producer, e.g. the thumbnail plugin's WebP encoder, and its decoder before the verifier.

Each frame waits for the detector's result on it. A frame is dropped, with no result, if:

- the detector skipped it, timed out, or is disconnected;
- its result names no detection of the class (a detector that drops a frame with no result, as detection-qnn does with `early_discard`, found nothing);
- no result comes within `timeout`;
- the crop and its result are larger than `max_width` x `max_height` allows.

The box is normalized, grown to whole pixels and clipped to the frame. The engine holds one frame at a time, so later frames wait at the server while it waits on the detector.

The detection engine can be the verifier, checking each crop with RF-DETR: see "As a verifier after detection-crop" in its README.

## The reintegration producer

An engine can only send in answer to an input. So without the producer, a crop that was lost, timed out, or made while the link was down would never reach the verifier, and no later frame would carry it. The `reintegration` producer (`[[engine.producers]]` in `detection-crop.plugin.toml`) sends the queued crops the verifier doesn't have yet, best first, whenever the flow has room for them, whether or not frames come in. A flow names it `local.<detection-crop>.reintegration`. It runs in the engine's process and publishes on `STEELEAGLE_ENGINE_ENDPOINT.reintegration`. Route it to the same place as the engine's output.

Uriel's client keeps only a producer's newest input until the flow has room for it, and an input replaced there never enters the flow. So the producer keeps one crop at a time waiting to be admitted. The next crop goes once an event shows the last one entered the flow. If a better crop is queued first, it takes the waiting one's place.

## The send queue

A crop leaves the queue only on the verifier's `RESULT` event for an input it went out as: the verifier finished it. `ACCEPTED` is not enough. For a remote verifier it means only that the remote server got the crop. A busy or stuck verifier then produces `ACCEPTED`, `SENT`, `TIMED_OUT` without ever taking it. Until its `RESULT`:

| Event or condition | Effect |
| --- | --- |
| `SKIPPED` / `TIMED_OUT` for the crop's latest carrier | the crop waits to be sent again |
| `DISCONNECTED`, or `LOST` (its `RESULT` may be among the lost) | every crop in flight waits to be sent again |
| `resend_after_ms` without word, e.g. dropped by an encoder before reaching the verifier, or never admitted | the crop waits to be sent again |
| more than `max_queue` crops | the crop of the least confident detection is dropped, in flight or not |

A crop sent again may reach the verifier twice; that is the price of never losing one.

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
| `resend_after_ms` | `5000` | Wait for a sent crop's `RESULT` before sending it again; keep it above the verifier's edge timeout plus its processing time |

## Development

```
pixi run -e test test   # all tests, including ones through a real Uriel remote crossing
```
