# Detection Crop

SteelEagle engine plugin that crops each camera frame (`RawFrame`) to the highest-scoring detection of a class that a detection engine on the same daemon found in it, and sends the crop on in place of the frame. The crop keeps the frame's id, timestamp and pose, and replaces its input, which keeps the input's id.

The engine subscribes to the detector's events, so the flow sends frames to both engines, and the crop engine's outgoing edges carry the crops:

```
camera ──► detection
   └─────► detection-crop ──► next engine
```

Each frame waits for the detector's result on it. A frame is dropped if:

- the detector skipped it, timed out, or is disconnected;
- its result names no detection of the class (a detector that drops a frame with no result, as detection-qnn does with `early_discard`, found nothing);
- no result comes within `timeout`;
- the crop is larger than `max_width` x `max_height`.

The box is normalized, grown to whole pixels and clipped to the frame. The engine holds one frame at a time, so later frames wait at the server while it waits on the detector.

| Arg | Default | Description |
| --- | --- | --- |
| `detector` | | Required: service name of the detection engine on this daemon |
| `class` | | Required: class to crop to, as the detector names it, ignoring case |
| `timeout` | `2000` | Longest to wait for the detector's result on a frame, in milliseconds |
| `max_width` | `1920` | Widest crop sent, in pixels |
| `max_height` | `1080` | Tallest crop sent, in pixels |

## Development

```
pixi run -e test test   # all tests, including one through a real Uriel server
```
