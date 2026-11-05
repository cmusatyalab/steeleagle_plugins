import queue
from pathlib import Path

import pytest

from recorder.engine import enqueue
from recorder.main import ConfigError, default_directory, parse_args


def test_parse_args_defaults():
    a = parse_args(None, env={})
    assert (
        a["engines"] == ["yolo"] and a["image_width"] == 320 and a["idle_timeout"] == 60
    )
    assert a["directory"] == str(default_directory({}))


def test_parse_args_keeps_given_values():
    a = parse_args(
        '{"directory": "/r", "engines": ["a", "b"], "image_width": 0}', env={}
    )
    assert (a["directory"], a["engines"], a["image_width"]) == ("/r", ["a", "b"], 0)


def test_default_directory():
    assert default_directory({"STEELEAGLE_FILES": "/data/steeleagle/files"}) == Path(
        "/data/steeleagle/logs/mcap"
    )
    assert default_directory({"XDG_DATA_HOME": "/x"}) == Path("/x/steeleagle/logs/mcap")
    assert (
        default_directory({})
        == Path("~/.local/share/steeleagle/logs/mcap").expanduser()
    )


def test_parse_args_rejects_bad_values():
    for bad in (
        '{"image_width": -1}',
        '{"idle_timeout": 0}',
        '{"engines": [""]}',
        '{"engines": "yolo"}',
    ):
        with pytest.raises(ConfigError):
            parse_args(bad, env={})


def test_full_queue_drops_without_blocking():
    q = queue.Queue(maxsize=1)
    assert enqueue(q, "a") is True
    assert enqueue(q, "b") is False
    assert q.get_nowait() == "a"


def test_byte_queue_bounds_bytes():
    from recorder.engine import ByteQueue

    q = ByteQueue(max_bytes=10)
    assert enqueue(q, b"12345678") is True
    assert enqueue(q, b"123") is False  # would exceed 10 bytes
    assert enqueue(q, b"12") is True
    assert q.get(timeout=0) == b"12345678"
    assert enqueue(q, b"123") is True  # room again once taken off
