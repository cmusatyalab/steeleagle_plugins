from google.protobuf import descriptor_pb2

from recorder.protobuf import message_class, protobuf_schema
from steeleagle_protocol.v1.messages.result import result_pb2
from steeleagle_protocol.v1.messages.telemetry import telemetry_pb2


def test_resolves_known_types():
    assert (
        message_class("steeleagle_protocol/v1/messages/result/DetectionResult")
        is result_pb2.DetectionResult
    )
    assert (
        message_class("steeleagle_protocol/v1/messages/telemetry/Telemetry")
        is telemetry_pb2.Telemetry
    )
    assert message_class("nope/Thing") is None


def test_schema_contains_dependencies():
    s = protobuf_schema(telemetry_pb2.Telemetry)
    fds = descriptor_pb2.FileDescriptorSet.FromString(bytes(s.data))
    names = [f.name for f in fds.file]
    assert "google/protobuf/timestamp.proto" in names
    assert "steeleagle_protocol/v1/common/common.proto" in names
    # Dependencies come before the files that import them, once each.
    assert names.index("steeleagle_protocol/v1/common/common.proto") < names.index(
        "steeleagle_protocol/v1/messages/telemetry/telemetry.proto"
    )
    assert len(names) == len(set(names))
    assert s.name == "steeleagle_protocol.v1.messages.telemetry.Telemetry"
    assert s.encoding == "protobuf"
