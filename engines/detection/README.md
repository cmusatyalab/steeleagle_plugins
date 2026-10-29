# Object Detection

A SteelEagle engine plugin that detects objects in camera frames with YOLO26 or RF-DETR. It takes `RawFrame`s and reports a `DetectionResult` (class, score, normalized box) for each, forwarding the frame unchanged. Detections can be limited to some classes and to a geofence.

## Args

| Arg | Default | Description |
| --- | --- | --- |
| `model` | `yolo26x.pt` | Model file in the daemon's models directory. `.pt` is Ultralytics YOLO (`yolo26n/s/m/l/x.pt` are downloaded if absent); `.pth` is RF-DETR; `rfdetr` is the stock pretrained RF-DETR. |
| `rfdetr_size` | `large` | RF-DETR size (`nano`, `small`, `medium`, `large`); a `.pth` file does not record it. |
| `rfdetr_labels` | `[]` | Class names of a custom RF-DETR checkpoint, in id order. |
| `only_classes` | `[]` | Class names to report, ignoring case. Empty reports all. |
| `confidence` | `0.5` | Confidence threshold. |
| `geofence` | `""` | KML file (e.g. from Google Maps) in the daemon's files directory. Only detections whose estimated location is inside it are reported. |
| `hfov`, `vfov` | `69`, `43` | Camera field of view, in degrees. |

Transfer models and the KML to the daemon with its `TransferService`.

## Location estimate

The geofence tests where the *object* is, not where the vehicle is, so the plugin estimates each detection's position on the ground:

1. Take the bottom center of the box, where the object most likely meets the ground.
2. Turn that pixel into a line of sight with a pinhole camera model, using `hfov` and `vfov`.
3. Aim the line of sight with the gimbal pitch (0° level, -90° straight down) and the vehicle heading (clockwise from north).
4. Follow it down to the ground, assumed flat at the vehicle's start height. From 30 m up with the gimbal 45° down, that is 30 m ahead.
5. Move that distance and bearing from the vehicle's GPS position to get a latitude and longitude, which is tested against the KML polygons.

```
   drone ●  30 m up
         |\
         | \  line of sight, 45° down
         |  \
 ground -+---●-- estimated object position
         <30 m>
```

Edge cases:
- **No gimbal pose in the frame:** the vehicle's own position is used.
- **Line of sight never reaches the ground** (at or above the horizon): there is no estimate, so the detection is dropped when a geofence is set and kept otherwise.

Accuracy assumes a level vehicle (the telemetry has no vehicle pitch or roll), flat terrain, no gimbal roll, and correct `hfov`/`vfov`. It is best at moderate distances on flat ground.

## Older GPUs

The default environment installs torch built for CUDA 13, which has no kernels
for Pascal GPUs (e.g. GTX 1080 Ti) and needs a recent NVIDIA driver. The
`cuda126` environment installs torch built for CUDA 12.6 instead. Select it in
the eagled config with `environment = "cuda126"` in the service's `[services.plugin]`
table, or run it directly with `pixi run -e cuda126 start`. The plugin code is
the same in both. Pascal GPUs have no fast FP16, so prefer a smaller model such
as `yolo26s.pt` over the default `yolo26x.pt`.

## Development

```
pixi run -e test test            # all tests
pixi run -e test test -m "not model"   # skip the tests that download and run real weights
```

The end-to-end test sends frames through a real Uriel server, client and producer.
