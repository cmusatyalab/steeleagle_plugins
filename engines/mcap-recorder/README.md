# MCAP Recorder

A SteelEagle engine plugin that records what vehicles send during a mission into MCAP files, one per vehicle, for replay and postmortem analysis in [Foxglove](https://foxglove.dev). It records the telemetry and camera frames the flow sends it, and the results of the engines it is told to watch. Detection results are drawn as boxes on the frames they were computed on.

## Placing it in a flow

The recorder drops every input after recording it, so it must be a leaf of the flow: on its own branch, never in line before another engine. A typical flow sends it:

- the vehicle's telemetry producer (`<vehicle>.telemetry`), and
- the H.264 encoder's output, on a branch next to the decoder that feeds the detector.

A frame keeps its input id through the decoder, so the detector's results are matched to the H.264 frames the recorder stored. The recorder copies each input and returns at once; recording happens on another thread, so it never slows the producer. If that thread falls behind, inputs are dropped with a warning.

| Arg | Default | Description |
| --- | --- | --- |
| `directory` | `""` | Where recordings go; empty for `logs/mcap` in the orchestrator's data directory, normally `~/.local/share/steeleagle/logs/mcap` |
| `engines` | `["detection"]` | Engines on this daemon whose results to record, qualified with `STEELEAGLE_LOCAL_QUALIFIER` |
| `image_width` | `320` | Width to store JPEG and raw frames at, aspect ratio kept; `0` for full size. H.264 is stored as it is |
| `idle_timeout` | `60` | Seconds without input from a vehicle before its recording is closed |

Only engines on the recorder's own daemon can be watched; results of engines on the vehicle's daemon are not recorded.

## Recordings

A vehicle's recording starts with its first input and is closed after `idle_timeout` seconds with none. Its next input starts a new one. Since the default flow installed after a mission sends the recorder nothing, each mission ends up in its own file: `<vehicle>_<YYYY-MM-DD_HH-MM-SS>.mcap`, with `_2`, `_3`… added if that name is taken. Vehicle names include the daemon's IP, e.g. `100.64.0.1.canary`.

| Topic | Schema | From |
| --- | --- | --- |
| `/<vehicle>/location` | `foxglove.LocationFix` | Telemetry global position; heading in radians, velocity in ENU |
| `/<vehicle>/telemetry` | `steeleagle_protocol.v1.messages.telemetry.Telemetry` (protobuf) | Every Telemetry message |
| `/<vehicle>/camera/<camera_id>` | `foxglove.CompressedVideo` | H.264 `EncodedFrame`s, untouched |
| `/<vehicle>/camera/<camera_id>` | `foxglove.CompressedImage` (JPEG) | JPEG `EncodedFrame`s and `RawFrame`s, resized to `image_width` |
| `/<vehicle>/camera/<camera_id>/detections/<engine>` | `foxglove.ImageAnnotations` | `DetectionResult`s, stamped with their frame's time |
| `/<vehicle>/results/<engine>` | the result's protobuf type | Every result of a watched engine; types with no known schema are JSON `{"payload_type", "data_base64"}` |

Messages are stamped with their own `timestamp` when they have one, else with the time they were received.

H.264 recording starts at the first keyframe with an SPS, so a recording that joins a stream can miss up to one keyframe interval. A frame the recorder misses is not re-keyed (the encoder only re-keys for the decoder's losses), so the video smears until the next keyframe. Shorten the encoder's keyframe interval in the flow if that matters.

### Unfinished recordings

eagled stops plugins with SIGKILL, so a recording still open when the daemon stops or crashes is left without its summary. Recordings are flushed every 2 seconds, so little is lost; repair such a file with the [mcap CLI](https://mcap.dev/guides/cli):

```bash
mcap recover in.mcap -o out.mcap
```

## Viewing in Foxglove

Open a vehicle's recording in Foxglove, then:

- add a **Map** panel showing `/<vehicle>/location`;
- add an **Image** panel on `/<vehicle>/camera/<camera_id>`, and turn on `/<vehicle>/camera/<camera_id>/detections/<engine>` under **Image annotations**;
- leave the Image panel's **Sync timestamps** setting off. With it on, the panel draws a frame only when every annotation topic has a message with the same timestamp, and since detectors don't run on every frame, nothing plays.

Each vehicle's mission is a separate file. Whether Foxglove can open several vehicles' files on one timeline has not been checked yet.

## Development

```bash
pixi run -e test test   # unit and end-to-end tests
```

`commands.sh` has a manual run line.
