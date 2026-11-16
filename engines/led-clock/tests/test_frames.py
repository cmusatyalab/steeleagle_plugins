from led_clock.frames import FIELDS, CsvWriter, read_rows

ROW = {
    "seq": 3,
    "recv_ns": 1_000,
    "fermi_ts_ns": 900,
    "tick": 7,
    "display_ns": 500,
    "latency_ms": 0.0005,
    "fermi_latency_ms": 0.0004,
    "confidence": 0.8,
    "step_ok": True,
}
UNDECODED = {name: None for name in FIELDS} | {"seq": 4, "recv_ns": 2_000}


def test_rows_read_back_as_written(tmp_path):
    path = tmp_path / "frames.csv"
    writer = CsvWriter(path)
    writer.write(ROW)
    writer.write(UNDECODED)
    writer.close()
    assert read_rows(path) == [ROW, UNDECODED]


def test_a_reopened_file_is_appended_to_with_one_header(tmp_path):
    path = tmp_path / "frames.csv"
    for row in (ROW, UNDECODED):
        writer = CsvWriter(path)
        writer.write(row)
        writer.close()
    assert read_rows(path) == [ROW, UNDECODED]
    assert path.read_text().count("seq,") == 1


def test_the_directory_is_made(tmp_path):
    path = tmp_path / "engine" / "frames.csv"
    writer = CsvWriter(path)
    writer.write(ROW)
    writer.close()
    assert read_rows(path) == [ROW]
