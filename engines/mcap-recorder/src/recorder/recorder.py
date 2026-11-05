"""Routes each input and result into its vehicle's MCAP topics.

Inputs are Telemetry and camera frames (H.264 or JPEG EncodedFrames, and
RawFrames). Results come from the engines the recorder subscribes to; a
DetectionResult is also drawn on the frame it was computed on, matched by
input id, which a filter (e.g. the H.264 decoder) keeps when it replaces a
frame."""

import dataclasses
import io
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal

import uriel
from google.protobuf.message import DecodeError
from foxglove.messages import CompressedImage, CompressedVideo
from PIL import Image

from recorder import convert, h264
from recorder.files import VehicleFiles
from recorder.protobuf import message_class
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.result import result_pb2
from steeleagle_protocol.v1.messages.telemetry import telemetry_pb2

logger = logging.getLogger("recorder")

TELEMETRY_TYPE = "steeleagle_protocol/v1/messages/telemetry/Telemetry"
ENCODED_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/EncodedFrame"
RAW_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/RawFrame"
DETECTION_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/DetectionResult"


@dataclass(frozen=True)
class Item:
    """An input, or a result event of a subscribed engine, copied out of
    uriel's shared memory for the writer thread."""

    kind: Literal["input", "event"]
    id: uriel.InputId
    payload_type: str
    data: bytes
    engine: str  # the qualified engine of an event; "" for an input
    received_ns: int  # wall clock


@dataclass(frozen=True)
class _Frame:
    ns: int
    width: int
    height: int
    camera_id: int
    received_ns: int


def vehicle_of(producer: str) -> str | None:
    """The vehicle a producer belongs to: its name up to the last '.'."""
    vehicle, dot, _ = producer.rpartition(".")
    return vehicle if dot and vehicle else None


class Recorder:
    def __init__(
        self,
        files: VehicleFiles,
        engines: dict[str, str],
        image_width: int,
        frame_memory_s: float = 5.0,
    ):
        self.files = files
        self.engines = engines
        self.image_width = image_width
        self.frame_memory_ns = int(frame_memory_s * 1e9)
        self._frames: OrderedDict[uriel.InputId, _Frame] = OrderedDict()
        self._h264_sizes: dict[tuple[str, int], tuple[int, int]] = {}
        self._early: OrderedDict[uriel.InputId, tuple] = OrderedDict()
        self._warned: set[str] = set()

    def handle(self, item: Item) -> None:
        """Records `item`; logs and skips anything it can't record."""
        # Some engines (e.g. detection-qnn) write a type's name with dots.
        if "." in item.payload_type:
            item = dataclasses.replace(
                item, payload_type=item.payload_type.replace(".", "/")
            )
        try:
            vehicle = vehicle_of(item.id.producer)
            if vehicle is None:
                self._warn_once(
                    f"producer {item.id.producer!r} names no vehicle; skipping it"
                )
                return
            if item.kind == "input":
                self._input(vehicle, item)
            elif item.data and item.engine in self.engines:
                self._result(vehicle, item)
        except Exception:
            logger.exception(
                "could not record %s from %s", item.payload_type, item.id.producer
            )

    def forget(self, vehicle: str) -> None:
        """Forgets the vehicle's H.264 streams when its recording closes, so
        its next recording starts at a keyframe again."""
        for key in [k for k in self._h264_sizes if k[0] == vehicle]:
            del self._h264_sizes[key]

    def _input(self, vehicle: str, item: Item) -> None:
        if item.payload_type == TELEMETRY_TYPE:
            tel = telemetry_pb2.Telemetry.FromString(item.data)
            ns = convert.message_time_ns(tel.timestamp, item.received_ns)
            self.files.log_protobuf(
                vehicle, f"/{vehicle}/telemetry", telemetry_pb2.Telemetry, item.data, ns
            )
            fix = convert.location_fix(tel, vehicle, ns)
            if fix is not None:
                self.files.log(vehicle, f"/{vehicle}/location", fix, ns)
        elif item.payload_type == ENCODED_FRAME_TYPE:
            self._encoded_frame(
                vehicle, item, frame_pb2.EncodedFrame.FromString(item.data)
            )
        elif item.payload_type == RAW_FRAME_TYPE:
            frame = frame_pb2.RawFrame.FromString(item.data)
            image = convert.raw_frame_image(frame)
            if image is None:
                logger.warning(
                    "%s: RawFrame of %d bytes is not a %dx%d RGB frame; skipping it",
                    item.id.producer,
                    len(frame.data),
                    frame.h_res,
                    frame.v_res,
                )
                return
            self._image(vehicle, item, frame.camera_id, frame.timestamp, image)
        else:
            self._warn_once(
                f"input of unexpected type {item.payload_type!r}; skipping it"
            )

    def _encoded_frame(
        self, vehicle: str, item: Item, frame: frame_pb2.EncodedFrame
    ) -> None:
        camera = frame.camera_id
        if frame.codec == frame_pb2.CODEC_H264:
            key = (vehicle, camera)
            size = h264.sps_size(frame.encoded_data)
            if size is not None:
                self._h264_sizes[key] = size
            size = self._h264_sizes.get(key)
            if size is None:
                # Joined mid-stream: nothing decodes before an SPS and keyframe.
                return
            ns = convert.message_time_ns(frame.timestamp, item.received_ns)
            video = CompressedVideo(
                timestamp=convert.fox_timestamp(ns),
                frame_id=f"camera{camera}",
                data=frame.encoded_data,
                format="h264",
            )
            self.files.log(vehicle, f"/{vehicle}/camera/{camera}", video, ns)
            self._remember(item, _Frame(ns, *size, camera, item.received_ns))
        elif frame.codec == frame_pb2.CODEC_JPEG:
            image = Image.open(io.BytesIO(frame.encoded_data))
            self._image(vehicle, item, camera, frame.timestamp, image)
        else:
            self._warn_once(f"EncodedFrame with codec {frame.codec}; skipping it")

    def _image(self, vehicle, item, camera, timestamp, image) -> None:
        jpeg, width, height = convert.thumbnail_jpeg(image, self.image_width)
        ns = convert.message_time_ns(timestamp, item.received_ns)
        msg = CompressedImage(
            timestamp=convert.fox_timestamp(ns),
            frame_id=f"camera{camera}",
            data=jpeg,
            format="jpeg",
        )
        self.files.log(vehicle, f"/{vehicle}/camera/{camera}", msg, ns)
        self._remember(item, _Frame(ns, width, height, camera, item.received_ns))

    def _remember(self, item: Item, frame: _Frame) -> None:
        self._frames[item.id] = frame
        self._frames.move_to_end(item.id)
        self._evict(self._frames, item.received_ns, lambda f: f.received_ns)
        early = self._early.pop(item.id, None)
        if early is not None:
            vehicle, engine, result, received_ns = early
            if item.received_ns - received_ns <= self.frame_memory_ns:
                self._draw(vehicle, engine, result, frame)

    def _result(self, vehicle: str, item: Item) -> None:
        engine = self.engines[item.engine]
        cls = message_class(item.payload_type)
        frame = self._frames.get(item.id)
        if (
            frame is not None
            and item.received_ns - frame.received_ns > self.frame_memory_ns
        ):
            frame = None
        ns = frame.ns if frame is not None else item.received_ns
        if cls is None:
            self._warn_once(
                f"{engine}: result type {item.payload_type!r} has no schema; logging it as JSON"
            )
        self.files.log_protobuf(
            vehicle,
            f"/{vehicle}/results/{engine}",
            cls,
            item.data,
            ns,
            payload_type=item.payload_type,
        )
        if item.payload_type != DETECTION_RESULT_TYPE:
            return
        try:
            result = result_pb2.DetectionResult.FromString(item.data)
        except DecodeError:
            logger.warning(
                "%s: could not parse a DetectionResult; not drawing it", engine
            )
            return
        if frame is None:
            # The result can arrive before the recorder has its frame; draw
            # it when the frame comes, if it does within the frame memory.
            self._early[item.id] = (vehicle, engine, result, item.received_ns)
            self._evict(self._early, item.received_ns, lambda e: e[3])
            return
        self._draw(vehicle, engine, result, frame)

    def _draw(self, vehicle, engine, result, frame: _Frame) -> None:
        annotations = convert.detection_annotations(
            result, frame.ns, frame.width, frame.height
        )
        self.files.log(
            vehicle,
            f"/{vehicle}/camera/{frame.camera_id}/detections/{engine}",
            annotations,
            frame.ns,
        )

    def _evict(self, entries: OrderedDict, now_ns: int, received) -> None:
        """Drops the oldest entries, received more than the frame memory
        before `now_ns`."""
        oldest = now_ns - self.frame_memory_ns
        while entries and received(next(iter(entries.values()))) < oldest:
            entries.popitem(last=False)

    def _warn_once(self, message: str) -> None:
        if message not in self._warned:
            self._warned.add(message)
            logger.warning(message)
