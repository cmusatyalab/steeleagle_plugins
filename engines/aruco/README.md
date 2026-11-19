# ArUco Markers

A SteelEagle engine plugin that finds ArUco fiducial markers in camera frames with OpenCV. It takes `RawFrame`s and reports a `DetectionResult` with a detection per marker: class `aruco_<id>` (e.g. `aruco_0`), score 1, and the normalized box around its four corners. The frame is forwarded unchanged or, with `early_discard`, dropped if it has no markers.

It replaces the Gabriel `aruco-engine` of the old backend, keeping its default dictionary and class names, so a mission that looked for `aruco_0` still can, e.g. with the `DetectionFound` event or the `Track` action naming this engine. Unlike the old engine, it does not write detections to Redis: they reach missions and the GCS as the engine's results.

## Args

| Arg | Default | Description |
| --- | --- | --- |
| `dictionary` | `DICT_ARUCO_ORIGINAL` | OpenCV predefined dictionary the markers are from, e.g. `DICT_4X4_50`, `DICT_6X6_250`, `DICT_APRILTAG_36h11`. An unknown name fails at startup with the list of valid ones. |
| `only_ids` | `[]` | Marker ids to report. Empty reports all. |
| `max_output_size` | `65536` | Largest result sent, in bytes. |
| `early_discard` | `false` | Drop frames with no markers instead of forwarding them. Their empty result still reaches subscribers. |

## Development

```sh
pixi run -e test test     # unit and end-to-end tests; markers are drawn with cv2.aruco
pixi run start            # what eagled runs, given STEELEAGLE_PLUGIN etc.
```
