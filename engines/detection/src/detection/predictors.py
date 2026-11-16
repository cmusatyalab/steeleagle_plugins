"""Detection backends: Ultralytics YOLO and RF-DETR.

Both take an RGB image and return the same Detected list, so the engine does
not care which one is running.

Each model runs on a square of a fixed size (its input size), and stretches
any other image to it: RF-DETR to its resolution, ignoring the aspect ratio;
YOLO to its imgsz, keeping it. A small image, such as a crop, is blown up to
fill that square, and RF-DETR stretches a tall crop to a square too.
LetterboxPredictor (the letterbox arg) instead scales the image evenly until
its longer side is the input size, up or down, and centers it on a gray square
of that size, so the model never stretches it.
"""

from dataclasses import dataclass, replace

import numpy as np
import supervision as sv
from PIL import Image

from detection.config import RFDETR_SIZE_CLASSES, RFDETR_STOCK_MODEL
from detection.paths import resolve


@dataclass(frozen=True)
class Detected:
    class_name: str
    score: float
    box: tuple[
        float, float, float, float
    ]  # y_min, x_min, y_max, x_max, as fractions of the image


# Ultralytics' default predict size, when a YOLO model does not record one
YOLO_DEFAULT_IMGSZ = 640
# The gray letterbox pads with: Ultralytics' letterbox fill, which is close to
# the ImageNet mean RF-DETR normalizes by, so the padding reads as nothing.
PAD_VALUE = 114


def keep_classes(detected, classes):
    """The detections of the named classes, ignoring case; all of them if no
    class is named."""
    if not classes:
        return detected
    wanted = {name.strip().casefold() for name in classes}
    return [d for d in detected if d.class_name.casefold() in wanted]


def from_supervision(detections, width, height):
    """The Detected list for a supervision.Detections of an image of this size."""
    names = detections.data.get("class_name", [])
    return [
        Detected(
            class_name=str(name),
            score=float(score),
            box=(y_min / height, x_min / width, y_max / height, x_max / width),
        )
        for (x_min, y_min, x_max, y_max), score, name in zip(
            detections.xyxy, detections.confidence, names
        )
    ]


def pad_to_square(rgb, size):
    """`rgb` centered on a gray square whose side is `size`, or the image's
    longer side if that is longer; and the (top, left) of the image in it."""
    height, width = rgb.shape[:2]
    side = max(size, height, width)
    top, left = (side - height) // 2, (side - width) // 2
    padded = np.full((side, side, rgb.shape[2]), PAD_VALUE, dtype=np.uint8)
    padded[top : top + height, left : left + width] = rgb
    return padded, top, left


def _clip(fraction):
    return min(max(fraction, 0.0), 1.0)


def scale_to_fit(rgb, size):
    """`rgb` scaled evenly so that its longer side is `size` pixels."""
    height, width = rgb.shape[:2]
    scale = size / max(height, width)
    if scale == 1:
        return rgb
    new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    # Bilinear up; averaging every pixel down, so a big frame does not alias
    resample = Image.Resampling.BILINEAR if scale > 1 else Image.Resampling.BOX
    return np.asarray(Image.fromarray(rgb).resize(new_size, resample))


def unpad(detected, side, top, left, width, height):
    """The detections `detected` in a padded square of `side` (pad_to_square),
    with boxes as fractions of the `width` x `height` image at (top, left) in
    it. Boxes are clipped to the image, and a detection wholly in the padding
    is left out."""
    out = []
    for d in detected:
        y_min, x_min, y_max, x_max = d.box
        box = (
            _clip((y_min * side - top) / height),
            _clip((x_min * side - left) / width),
            _clip((y_max * side - top) / height),
            _clip((x_max * side - left) / width),
        )
        if box[2] > box[0] and box[3] > box[1]:
            out.append(replace(d, box=box))
    return out


class LetterboxPredictor:
    """Runs `predictor` on each image letterboxed to its input size: scaled
    evenly to fit it, then padded to its square, rather than letting the model
    resize the image to it. Boxes are fractions of the image, as unscaled
    boxes would be."""

    def __init__(self, predictor):
        self.predictor = predictor
        self.input_size = predictor.input_size

    def predict(self, rgb):
        scaled = scale_to_fit(rgb, self.input_size)
        height, width = scaled.shape[:2]
        padded, top, left = pad_to_square(scaled, self.input_size)
        detected = self.predictor.predict(padded)
        return unpad(detected, padded.shape[0], top, left, width, height)


class YoloPredictor:
    def __init__(self, weights, confidence):
        from ultralytics import YOLO

        # A stock name that is not on disk yet is downloaded to `weights`.
        self.model = YOLO(str(weights))
        self.confidence = confidence

    @property
    def input_size(self):
        """The side of the square the model runs on."""
        imgsz = self.model.overrides.get("imgsz") or YOLO_DEFAULT_IMGSZ
        return max(imgsz) if isinstance(imgsz, (list, tuple)) else int(imgsz)

    def predict(self, rgb):
        bgr = np.ascontiguousarray(rgb[..., ::-1])  # Ultralytics reads arrays as BGR
        results = self.model.predict(bgr, conf=self.confidence, verbose=False)
        height, width = rgb.shape[:2]
        return from_supervision(
            sv.Detections.from_ultralytics(results[0]), width, height
        )


class RfDetrPredictor:
    def __init__(self, weights, size, confidence, labels):
        import rfdetr

        if weights:
            # The checkpoint records the variant it was trained as (including
            # the separately packaged XLarge and 2XLarge), its resolution and
            # its classes, so it builds its own model whatever `size` says.
            # trust_checkpoint allows a full unpickle when a checkpoint cannot
            # be loaded safely, as Ultralytics already does for .pt files.
            self.model = rfdetr.RFDETR.from_checkpoint(
                str(weights), trust_checkpoint=True
            )
        else:
            # The stock model of `size` downloads its own pretrained weights.
            model_class = getattr(rfdetr, RFDETR_SIZE_CLASSES[size])
            self.model = model_class()
        self.confidence = confidence
        self.labels = labels

    @property
    def input_size(self):
        """The side of the square the model runs on: the resolution its
        predict resizes every image to."""
        return int(self.model.model.resolution)

    def predict(self, rgb):
        detections = self.model.predict(rgb, threshold=self.confidence)
        if self.labels:
            # class_id can fall outside our names: RF-DETR reserves a
            # background slot past the last class, and its COCO checkpoints
            # use sparse 1-90 ids. Its own class_name is right for those.
            detections.data["class_name"] = np.array(
                [
                    self.labels[i] if 0 <= i < len(self.labels) else name
                    for i, name in zip(
                        detections.class_id, detections.data["class_name"]
                    )
                ],
                dtype=object,
            )
        height, width = rgb.shape[:2]
        return from_supervision(detections, width, height)


def load_predictor(config, models_dir):
    """The predictor `config` asks for. A model file is looked for in
    `models_dir`, where eagled saves transferred models."""
    predictor = _load(config, models_dir)
    return LetterboxPredictor(predictor) if config.letterbox else predictor


def _load(config, models_dir):
    if config.architecture == "rfdetr":
        weights = (
            None
            if config.model == RFDETR_STOCK_MODEL
            else resolve(models_dir, config.model)
        )
        if weights is not None and not weights.is_file():
            raise FileNotFoundError(
                f"RF-DETR weights {weights} were not transferred to the daemon"
            )
        return RfDetrPredictor(
            weights, config.rfdetr_size, config.confidence, config.rfdetr_labels
        )
    return YoloPredictor(resolve(models_dir, config.model), config.confidence)
