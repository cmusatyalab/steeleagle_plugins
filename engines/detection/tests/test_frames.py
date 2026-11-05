from datetime import datetime, timezone

import numpy as np
from conftest import make_frame
from PIL import Image

from detection.frames import FrameSaver, frame_file_name, producer_dir_name


def save(directory, *frames, producer="camera"):
    saver = FrameSaver(directory)
    for seq, frame in enumerate(frames):
        saver.submit(producer, seq, frame)
    saver.close()


def test_a_saved_frame_is_a_jpeg_of_its_pixels(tmp_path):
    frame = make_frame(width=64, height=48)
    pixels = np.zeros((48, 64, 3), dtype=np.uint8)
    pixels[:, :32] = (200, 40, 40)
    frame.data = pixels.tobytes()
    save(tmp_path, frame)
    (path,) = (tmp_path / "camera").iterdir()
    assert path.suffix == ".jpg"
    with Image.open(path) as image:
        assert image.format == "JPEG"
        assert image.size == (64, 48)
        saved = np.asarray(image.convert("RGB"), dtype=int)
    # Quality 95 keeps flat colors within a few levels
    assert np.abs(saved - pixels).mean() < 3


def test_each_producer_gets_a_directory_and_each_frame_a_file(tmp_path):
    save(tmp_path, make_frame(), make_frame(), producer="a")
    save(tmp_path, make_frame(), producer="b")
    assert len(list((tmp_path / "a").iterdir())) == 2
    assert len(list((tmp_path / "b").iterdir())) == 1


def test_a_frame_that_is_not_rgb_is_not_saved_and_later_frames_still_are(tmp_path):
    bad = make_frame()
    bad.channels = 4
    save(tmp_path, bad, make_frame())
    assert len(list((tmp_path / "camera").iterdir())) == 1


def test_frames_past_a_full_queue_are_skipped_and_counted(tmp_path):
    saver = FrameSaver(tmp_path, queue_size=1)
    # Hold the writer up so the queue fills
    saver._queue.put(("camera", 0, 0, 0, 3, b""))
    for seq in range(5):
        saver.submit("camera", seq, make_frame())
    assert saver.skipped >= 4
    saver.close()


def test_producer_names_cannot_leave_the_frames_directory():
    assert producer_dir_name("../../etc") == ".._.._etc"
    assert producer_dir_name("..") == "unknown"
    assert producer_dir_name("") == "unknown"
    assert producer_dir_name("drone-1.cam") == "drone-1.cam"


def test_file_names_sort_by_time_then_carry_the_seq():
    now = datetime(2026, 11, 5, 14, 30, 12, 123456, tzinfo=timezone.utc)
    assert frame_file_name(42, now) == "20261105T143012.123456_00000042.jpg"
