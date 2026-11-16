from types import SimpleNamespace

import pytest
from google.protobuf.message import DecodeError

from metrics import ENCODED_FRAME_TYPE, RAW_FRAME_TYPE, csv_file_path, parse_frame
from steeleagle_protocol.v1.messages.frame import frame_pb2


def make_input(message, payload_type=""):
    return SimpleNamespace(data=message.SerializeToString(), payload_type=payload_type)


@pytest.mark.parametrize("payload_type", [ENCODED_FRAME_TYPE, ENCODED_FRAME_TYPE.replace("/", "."), ""])
def test_encoded(payload_type):
    frame, kind, width, height = parse_frame(
        make_input(frame_pb2.EncodedFrame(id=7, encoded_data=b"abc"), payload_type))
    assert (frame.id, kind, width, height) == (7, "encoded", 0, 0)


@pytest.mark.parametrize("payload_type", [RAW_FRAME_TYPE, ""])
def test_raw(payload_type):
    raw = frame_pb2.RawFrame(id=3, data=b"\0" * 12, h_res=2, v_res=2, channels=3)
    frame, kind, width, height = parse_frame(make_input(raw, payload_type))
    assert (frame.id, kind, width, height) == (3, "raw", 2, 2)


def test_neither():
    with pytest.raises(DecodeError):
        parse_frame(SimpleNamespace(data=b"", payload_type=""))


def test_relative_csv_path(tmp_path, monkeypatch):
    monkeypatch.setenv("STEELEAGLE_FILES", str(tmp_path))
    assert csv_file_path("m/frames.csv") == tmp_path / "m" / "frames.csv"
    assert (tmp_path / "m").is_dir()
    assert csv_file_path(str(tmp_path / "a.csv")) == tmp_path / "a.csv"
