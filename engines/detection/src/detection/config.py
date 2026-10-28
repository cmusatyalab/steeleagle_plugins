"""The plugin's args, as the daemon passes them in STEELEAGLE_PLUGIN_ARGS."""

import json
from dataclasses import dataclass, field, fields

DEFAULT_MODEL = "yolo26x.pt"
DEFAULT_RFDETR_SIZE = "large"
DEFAULT_CONFIDENCE = 0.5
# The field of view of the cameras the old engine assumed.
DEFAULT_HFOV_DEG = 69.0
DEFAULT_VFOV_DEG = 43.0
DEFAULT_MAX_OUTPUT_BYTES = 1 << 16
MAX_FOV_DEG = 180.0

YOLO_SUFFIX = ".pt"
RFDETR_SUFFIX = ".pth"
# Stock pretrained RF-DETR of rfdetr_size, downloaded on first use.
RFDETR_STOCK_MODEL = "rfdetr"
RFDETR_SIZES = ("nano", "small", "medium", "large")


class ConfigError(Exception):
    pass


@dataclass
class Config:
    model: str = DEFAULT_MODEL
    rfdetr_size: str = DEFAULT_RFDETR_SIZE
    rfdetr_labels: list[str] = field(default_factory=list)
    only_classes: list[str] = field(default_factory=list)
    confidence: float = DEFAULT_CONFIDENCE
    geofence: str = ""
    hfov: float = DEFAULT_HFOV_DEG
    vfov: float = DEFAULT_VFOV_DEG
    max_output_size: int = DEFAULT_MAX_OUTPUT_BYTES

    @property
    def architecture(self):
        if self.model == RFDETR_STOCK_MODEL or self.model.endswith(RFDETR_SUFFIX):
            return "rfdetr"
        return "yolo"


def parse_args(content):
    """The Config for the JSON object `content`; any arg it leaves unset (or
    null) takes its default."""
    given = json.loads(content or "{}")
    known = {f.name for f in fields(Config)}
    config = Config(**{k: v for k, v in given.items() if k in known and v is not None})
    _validate(config)
    return config


def _validate(config):
    if config.architecture == "yolo" and not config.model.endswith(YOLO_SUFFIX):
        raise ConfigError(
            f"model {config.model!r} must end in {YOLO_SUFFIX} (YOLO), "
            f"{RFDETR_SUFFIX} (RF-DETR), or be {RFDETR_STOCK_MODEL!r}"
        )
    if config.rfdetr_size not in RFDETR_SIZES:
        raise ConfigError(
            f"rfdetr_size {config.rfdetr_size!r} must be one of {RFDETR_SIZES}"
        )
    if not 0 < config.confidence <= 1:
        raise ConfigError(f"confidence {config.confidence} must be in (0, 1]")
    for name in ("hfov", "vfov"):
        degrees = getattr(config, name)
        if not 0 < degrees < MAX_FOV_DEG:
            raise ConfigError(
                f"{name} {degrees} must be between 0 and {MAX_FOV_DEG} degrees"
            )
