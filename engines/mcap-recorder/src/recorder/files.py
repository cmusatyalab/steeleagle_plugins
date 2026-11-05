"""One MCAP file per vehicle, each with its own Foxglove context, closed when
the vehicle has been idle for a while.

A file is finalized only when it is closed: by idle close, or by close_all
when the engine stops. eagled stops plugins with SIGKILL, so a file open then
is left without its summary; `mcap recover` repairs it. Flushing finishes the
current chunk so little is lost."""

import base64
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import foxglove
from foxglove import channels as fox_channels
from foxglove.mcap import MCAPWriteOptions
from google.protobuf.message import Message

from recorder.protobuf import protobuf_schema

logger = logging.getLogger("recorder")

CHUNK_SIZE = 256 * 1024


@dataclass
class _File:
    path: Path
    context: foxglove.Context
    writer: object
    last_input: float
    channels: dict = field(default_factory=dict)


class VehicleFiles:
    def __init__(
        self,
        directory: Path,
        idle_timeout_s: float,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
    ):
        self.directory = Path(directory)
        self.idle_timeout_s = idle_timeout_s
        self.clock = clock
        self.now = now
        self._files: dict[str, _File] = {}

    def log(self, vehicle: str, topic: str, msg, log_time_ns: int) -> None:
        """Logs the foxglove.messages object `msg` on `topic` of the
        vehicle's file, on a channel of the message's own schema."""
        f = self._file(vehicle)
        channel = f.channels.get(topic)
        if channel is None:
            channel_class = getattr(fox_channels, type(msg).__name__ + "Channel")
            channel = f.channels[topic] = channel_class(topic, context=f.context)
        channel.log(msg, log_time=log_time_ns)

    def log_protobuf(
        self,
        vehicle: str,
        topic: str,
        cls: type[Message] | None,
        data: bytes,
        log_time_ns: int,
        payload_type: str = "",
    ) -> None:
        """Logs the serialized message `data` of class `cls` on `topic`.
        With no class (a payload type we have no schema for), logs it as JSON
        {"payload_type", "data_base64"}: the SDK has no schemaless binary
        channels, and Foxglove can show JSON."""
        f = self._file(vehicle)
        channel = f.channels.get(topic)
        if channel is None:
            if cls is None:
                channel = foxglove.Channel(topic, context=f.context)
            else:
                channel = foxglove.Channel(
                    topic,
                    schema=protobuf_schema(cls),
                    message_encoding="protobuf",
                    context=f.context,
                )
            f.channels[topic] = channel
        if cls is None:
            data = json.dumps(
                {
                    "payload_type": payload_type,
                    "data_base64": base64.b64encode(data).decode(),
                }
            ).encode()
        channel.log(data, log_time=log_time_ns)

    def close_idle(self) -> list[str]:
        """Closes the files of the vehicles idle for the idle timeout, and
        returns those vehicles."""
        now = self.clock()
        idle = [
            v
            for v, f in self._files.items()
            if now - f.last_input >= self.idle_timeout_s
        ]
        for vehicle in idle:
            logger.info(
                "%s idle for %ss; closing its recording", vehicle, self.idle_timeout_s
            )
            self._close(vehicle)
        return idle

    def flush(self) -> None:
        for f in self._files.values():
            f.writer.flush()

    def close_all(self) -> None:
        for vehicle in list(self._files):
            self._close(vehicle)

    def open_paths(self) -> dict[str, Path]:
        return {v: f.path for v, f in self._files.items()}

    def _file(self, vehicle: str) -> _File:
        f = self._files.get(vehicle)
        if f is None:
            path = self._new_path(vehicle)
            context = foxglove.Context()
            writer = foxglove.open_mcap(
                str(path),
                context=context,
                writer_options=MCAPWriteOptions(chunk_size=CHUNK_SIZE),
            )
            f = self._files[vehicle] = _File(path, context, writer, self.clock())
            logger.info("recording %s to %s", vehicle, path)
        f.last_input = self.clock()
        return f

    def _new_path(self, vehicle: str) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        stem = f"{vehicle}_{self.now().strftime('%Y-%m-%d_%H-%M-%S')}"
        path = self.directory / f"{stem}.mcap"
        n = 2
        while path.exists():
            path = self.directory / f"{stem}_{n}.mcap"
            n += 1
        return path

    def _close(self, vehicle: str) -> None:
        f = self._files.pop(vehicle)
        for channel in f.channels.values():
            channel.close()
        f.writer.close()
        logger.info("closed recording %s", f.path)
