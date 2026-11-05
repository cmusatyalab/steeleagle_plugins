"""Conversions from SteelEagle protobuf messages to Foxglove's schemas, for
the messages with a straightforward mapping."""

import io
import math

from foxglove.messages import (
    Color,
    ImageAnnotations,
    LocationFix,
    Point2,
    PointsAnnotation,
    PointsAnnotationType,
    TextAnnotation,
    Timestamp,
    Vector3,
)
from google.protobuf import timestamp_pb2
from PIL import Image

from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.result import result_pb2
from steeleagle_protocol.v1.messages.telemetry import telemetry_pb2

RGB_CHANNELS = 3
JPEG_QUALITY = 85

BOX_COLOR = Color(r=0.0, g=1.0, b=0.0, a=1.0)
BOX_THICKNESS = 2.0
LABEL_COLOR = Color(r=1.0, g=1.0, b=1.0, a=1.0)
LABEL_BACKGROUND = Color(r=0.0, g=0.0, b=0.0, a=0.6)
LABEL_FONT_SIZE = 14.0


def message_time_ns(ts: timestamp_pb2.Timestamp, received_ns: int) -> int:
    """The time of a message: its own timestamp if set, else when it was
    received."""
    if ts.seconds or ts.nanos:
        return ts.seconds * 1_000_000_000 + ts.nanos
    return received_ns


def fox_timestamp(ns: int) -> Timestamp:
    return Timestamp(sec=ns // 1_000_000_000, nsec=ns % 1_000_000_000)


def location_fix(
    tel: telemetry_pb2.Telemetry, vehicle: str, ns: int
) -> LocationFix | None:
    """The vehicle's global position as a LocationFix, or None if the
    telemetry has none. Heading is converted to radians, and the NEU velocity
    to ENU."""
    g = tel.position_info.global_position
    if not (g.HasField("latitude") and g.HasField("longitude")):
        return None
    extra = {}
    if g.HasField("heading"):
        extra["heading"] = math.radians(g.heading)
    v = tel.position_info.velocity_neu
    if all(v.HasField(f) for f in ("x_vel", "y_vel", "z_vel")):
        extra["velocity"] = Vector3(x=v.y_vel, y=v.x_vel, z=v.z_vel)
    return LocationFix(
        timestamp=fox_timestamp(ns),
        frame_id=vehicle,
        latitude=g.latitude,
        longitude=g.longitude,
        altitude=g.altitude,
        **extra,
    )


def thumbnail_jpeg(image: Image.Image, width: int) -> tuple[bytes, int, int]:
    """`image` as a JPEG at most `width` wide (aspect ratio kept, never
    upscaled; 0 for full size), with the JPEG's width and height."""
    if width > 0 and image.width > width:
        image = image.copy()
        image.thumbnail((width, image.height), Image.LANCZOS)
    if image.mode != "RGB":
        image = image.convert("RGB")
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue(), image.width, image.height


def raw_frame_image(frame: frame_pb2.RawFrame) -> Image.Image | None:
    """The RGB pixels of `frame` as an image, or None if they aren't a
    whole RGB frame of its size."""
    w, h = frame.h_res, frame.v_res
    if (
        frame.channels != RGB_CHANNELS
        or w == 0
        or h == 0
        or len(frame.data) != w * h * RGB_CHANNELS
    ):
        return None
    return Image.frombytes("RGB", (w, h), frame.data)


def detection_annotations(
    result: result_pb2.DetectionResult, ns: int, width: int, height: int
) -> ImageAnnotations:
    """A box and a "class score" label per detection, in pixels of a
    `width`x`height` image, stamped `ns` (the frame's time, so Foxglove draws
    them on that frame)."""
    ts = fox_timestamp(ns)
    points, texts = [], []
    for d in result.detections:
        x0, x1 = d.bbox.x_min * width, d.bbox.x_max * width
        y0, y1 = d.bbox.y_min * height, d.bbox.y_max * height
        points.append(
            PointsAnnotation(
                timestamp=ts,
                type=PointsAnnotationType.LineLoop,
                points=[
                    Point2(x=x0, y=y0),
                    Point2(x=x1, y=y0),
                    Point2(x=x1, y=y1),
                    Point2(x=x0, y=y1),
                ],
                outline_color=BOX_COLOR,
                thickness=BOX_THICKNESS,
            )
        )
        texts.append(
            TextAnnotation(
                timestamp=ts,
                position=Point2(x=x0, y=y0 - 4),
                text=f"{d.class_name} {d.score:.2f}",
                font_size=LABEL_FONT_SIZE,
                text_color=LABEL_COLOR,
                background_color=LABEL_BACKGROUND,
            )
        )
    return ImageAnnotations(points=points, texts=texts)
