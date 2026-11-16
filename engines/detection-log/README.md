# Detection Log

A SteelEagle engine plugin that saves the frames a detection engine on this daemon ran inference on, with the boxes it found drawn on them. It takes `RawFrame`s and drops every one, with no result. It does not run a model: it subscribes to the detector's events and draws the detector's `DetectionResult` for the same input on the frame.

Each frame is saved as two quality-75 JPEGs:

- `<files>/<engine>/raw/<seq>.jpeg`, the frame as it was received.
- `<files>/<engine>/out/<seq>.jpeg`, the frame with the detections drawn on it. Each detection is drawn as a red box, labelled with its class and score.

Where:

- `<files>` is the daemon's files directory (`STEELEAGLE_FILES`, or `/steeleagle/files` in a sandbox).
- `<engine>` is the whole qualified `STEELEAGLE_ENGINE_ENDPOINT`, e.g. `<files>/100.64.0.1.detection_log/`.
- `<seq>` is the sequence number of the frame's input. A repeated sequence number overwrites the earlier frame, e.g. after the producer restarts or with two producers.

| Arg | Default | Description |
| --- | --- | --- |
| `detector` | | Detection engine on this daemon, qualified with `STEELEAGLE_LOCAL_QUALIFIER` |
| `timeout` | `2000` | The longest to wait for the detector's result on a frame, in milliseconds |
| `refresh` | `false` | Remove everything already in `<files>/<engine>/` when the plugin starts, so only this run's frames are there |

## Flow

The engine must get the same inputs as the detector. There are two ways to set that up:

- Put the detector and this engine on edges from the same producer.
- Chain this engine after the detector. `detection` and `detection-qnn` forward every frame, unless `early_discard` is set; then frames with no detections are not saved.

The detector's result may arrive before or after the frame. For each frame:

| What the detector did | What is saved |
| --- | --- |
| Returned a `DetectionResult` | The frame, with its detections drawn |
| Dropped the frame with no result | The frame, with no boxes |
| Skipped the frame, timed out, or disconnected | Nothing |
| Sent no result before `timeout` | Nothing |

A disconnected detector's waiting frames are given up, and so is every new frame until it is heard from again. At most 32 frames wait on the detector at once; past that, the oldest is given up.

The engine reads and drops each frame right away and writes the JPEGs on a separate thread. If the disk falls behind, frames are skipped and a warning is logged. Every 30 s, a log line counts the frames saved and the frames not saved, with the reason for each. Nothing is deleted unless `refresh` is set: 720p at 10 fps fills roughly 1 GB every 10 minutes, with both JPEGs of each frame.

## Tests

```
pixi run -e test test
```

The end-to-end test sends frames through a real Uriel server, client and producer, to a stand-in detector and this engine.
