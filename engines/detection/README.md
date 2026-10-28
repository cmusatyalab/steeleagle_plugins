# Object Detection

A SteelEagle engine plugin that detects objects in camera frames with YOLO26 or RF-DETR. It takes `RawFrame`s and reports a `DetectionResult` (class, score, normalized box) for each, forwarding the frame unchanged. Detections can be limited to some classes and to a geofence.

## Args

| Arg | Default | Description |
| --- | --- | --- |
| `model` | `yolo26n.pt` | Model file in the daemon's models directory. `.pt` is Ultralytics YOLO (`yolo26n/s/m/l/x.pt` are downloaded if absent); `.pth` is RF-DETR; `rfdetr` is the stock pretrained RF-DETR. |
| `rfdetr_size` | `medium` | RF-DETR size (`nano`, `small`, `medium`, `large`); a `.pth` file does not record it. |
| `rfdetr_labels` | `[]` | Class names of a custom RF-DETR checkpoint, in id order. |
| `only_classes` | `[]` | Class names to report, ignoring case. Empty reports all. |
| `confidence` | `0.5` | Confidence threshold. |
| `geofence` | `""` | KML file (e.g. from Google Maps) in the daemon's files directory. Only detections whose estimated location is inside it are reported. |
| `hfov`, `vfov` | `69`, `43` | Camera field of view, in degrees. |

Transfer models and the KML to the daemon with its `TransferService`.

## Location estimate

The geofence tests where the object is, not where the vehicle is. The bottom center of the box is cast as a ray through a pinhole camera, rotated by the gimbal pitch and vehicle heading, and intersected with flat ground at the vehicle's height. A frame without a gimbal pose falls back to the vehicle's position. The estimate assumes a level vehicle and flat terrain, and ignores gimbal roll.

## Development

```
uv run pytest                  # all tests
uv run pytest -m "not model"   # skip the tests that download and run real weights
```

The end-to-end test sends frames through a real Uriel server, client and producer.
