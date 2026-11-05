"""Detection backends: Ultralytics YOLO and RF-DETR.

Both take an RGB image and return the same Detected list, so the engine does
not care which one is running.
"""

from dataclasses import dataclass

import numpy as np
import supervision as sv

from detection.config import RFDETR_SIZE_CLASSES, RFDETR_STOCK_MODEL
from detection.paths import resolve


@dataclass(frozen=True)
class Detected:
    class_name: str
    score: float
    box: tuple[
        float, float, float, float
    ]  # y_min, x_min, y_max, x_max, as fractions of the image


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


class YoloPredictor:
    def __init__(self, weights, confidence):
        from ultralytics import YOLO

        # A stock name that is not on disk yet is downloaded to `weights`.
        self.model = YOLO(str(weights))
        self.confidence = confidence

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
