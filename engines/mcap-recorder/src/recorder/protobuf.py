"""Our protobuf messages as MCAP channel schemas, so Foxglove can show every
field of the messages it has no schema of its own for."""

import functools

import foxglove
from google.protobuf import descriptor_pb2
from google.protobuf.message import Message

from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.result import result_pb2
from steeleagle_protocol.v1.messages.telemetry import telemetry_pb2

_MODULES = (telemetry_pb2, frame_pb2, result_pb2)


def _payload_type(cls: type[Message]) -> str:
    """The payload type uriel names `cls` by: its full name with '/'."""
    return cls.DESCRIPTOR.full_name.replace(".", "/")


_TYPES = {
    _payload_type(cls): cls
    for module in _MODULES
    for name in module.DESCRIPTOR.message_types_by_name
    for cls in (getattr(module, name),)
}


def message_class(payload_type: str) -> type[Message] | None:
    """The message class of the payload type `payload_type`, e.g.
    steeleagle_protocol/v1/messages/result/DetectionResult; None if unknown."""
    return _TYPES.get(payload_type)


@functools.cache
def protobuf_schema(cls: type[Message]) -> foxglove.Schema:
    """A protobuf channel schema for `cls`: a FileDescriptorSet of its file
    and every file it depends on, dependencies first."""
    fds = descriptor_pb2.FileDescriptorSet()
    seen = set()

    def add(file):
        if file.name in seen:
            return
        seen.add(file.name)
        for dep in file.dependencies:
            add(dep)
        file.CopyToProto(fds.file.add())

    add(cls.DESCRIPTOR.file)
    return foxglove.Schema(
        name=cls.DESCRIPTOR.full_name, encoding="protobuf", data=fds.SerializeToString()
    )
