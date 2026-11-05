import json
from datetime import datetime

from conftest import decode_protobuf, read_mcap
from foxglove.messages import LocationFix
from mcap.reader import make_reader

from recorder.files import VehicleFiles
from recorder.protobuf import protobuf_schema
from steeleagle_protocol.v1.messages.telemetry import telemetry_pb2

NOW = datetime(2026, 11, 5, 8, 0, 0)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_one_file_per_vehicle(tmp_path):
    files = VehicleFiles(tmp_path, 60, clock=lambda: 0, now=lambda: NOW)
    files.log("a.b", "/a.b/location", LocationFix(latitude=1), 1)
    files.log("c.d", "/c.d/location", LocationFix(latitude=2), 1)
    files.close_all()
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "a.b_2026-11-05_08-00-00.mcap",
        "c.d_2026-11-05_08-00-00.mcap",
    ]
    assert read_mcap(tmp_path / "a.b_2026-11-05_08-00-00.mcap")[0][:3] == (
        "/a.b/location",
        "foxglove.LocationFix",
        1,
    )


def test_creates_missing_directory(tmp_path):
    files = VehicleFiles(tmp_path / "x" / "y", 60, now=lambda: NOW)
    files.log("v", "/v/location", LocationFix(latitude=1), 1)
    files.close_all()
    assert (tmp_path / "x" / "y" / "v_2026-11-05_08-00-00.mcap").exists()


def test_idle_close_then_new_file(tmp_path):
    clock = Clock()
    times = iter([NOW, datetime(2026, 11, 5, 9, 0, 0)])
    files = VehicleFiles(tmp_path, 60, clock=clock, now=lambda: next(times))
    files.log("v", "/v/location", LocationFix(latitude=1), 1)
    first = files.open_paths()["v"]
    clock.t = 59
    assert files.close_idle() == []
    clock.t = 60
    assert files.close_idle() == ["v"]
    assert files.open_paths() == {}
    # The closed file is finalized: it has a summary section.
    with open(first, "rb") as f:
        assert make_reader(f).get_summary() is not None
    files.log("v", "/v/location", LocationFix(latitude=2), 2)
    assert files.open_paths()["v"].name == "v_2026-11-05_09-00-00.mcap"
    files.close_all()


def test_logging_keeps_a_vehicle_open(tmp_path):
    clock = Clock()
    files = VehicleFiles(tmp_path, 60, clock=clock, now=lambda: NOW)
    files.log("v", "/v/location", LocationFix(latitude=1), 1)
    clock.t = 50
    files.log("v", "/v/location", LocationFix(latitude=1), 2)
    clock.t = 100
    assert files.close_idle() == []
    files.close_all()


def test_reopen_same_second_gets_suffix(tmp_path):
    clock = Clock()
    files = VehicleFiles(tmp_path, 60, clock=clock, now=lambda: NOW)
    files.log("v", "/v/location", LocationFix(latitude=1), 1)
    clock.t = 60
    files.close_idle()
    files.log("v", "/v/location", LocationFix(latitude=1), 2)
    assert files.open_paths()["v"].name == "v_2026-11-05_08-00-00_2.mcap"
    files.close_all()
    assert len(read_mcap(tmp_path / "v_2026-11-05_08-00-00.mcap")) == 1


def test_protobuf_and_raw_channels(tmp_path):
    files = VehicleFiles(tmp_path, 60, now=lambda: NOW)
    tel = telemetry_pb2.Telemetry()
    tel.battery_info.percentage = 42
    files.log_protobuf(
        "v", "/v/telemetry", telemetry_pb2.Telemetry, tel.SerializeToString(), 5
    )
    files.log_protobuf("v", "/v/results/x", None, b"\x01\x02", 6, payload_type="x/Y")
    files.close_all()
    (t_topic, t_schema, _, t_data, t_enc), (r_topic, _, _, r_data, r_enc) = read_mcap(
        tmp_path / "v_2026-11-05_08-00-00.mcap"
    )
    assert (t_topic, t_schema, t_enc) == (
        "/v/telemetry",
        "steeleagle_protocol.v1.messages.telemetry.Telemetry",
        "protobuf",
    )
    schema = protobuf_schema(telemetry_pb2.Telemetry)
    decoded = decode_protobuf(schema.name, bytes(schema.data), t_data)
    assert decoded.battery_info.percentage == 42
    assert (r_topic, r_enc) == ("/v/results/x", "json")
    assert json.loads(r_data) == {"payload_type": "x/Y", "data_base64": "AQI="}
