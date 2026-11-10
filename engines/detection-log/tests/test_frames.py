import numpy as np
from conftest import make_frame, make_result
from PIL import Image

from detection_log.frames import FrameSaver, frame_file_name


def save(directory, *frames, result=None, first_seq=0):
    saver = FrameSaver(directory)
    for seq, frame in enumerate(frames, start=first_seq):
        saver.submit(seq, frame, result or make_result())
    saver.close()


def read(path):
    with Image.open(path) as image:
        assert image.format == "JPEG"
        return np.asarray(image.convert("RGB"), dtype=int)


def test_a_frame_without_detections_is_a_jpeg_of_its_pixels(tmp_path):
    save(tmp_path, make_frame(64, 48, color=(40, 160, 200)))
    saved = read(tmp_path / "0.jpg")
    assert saved.shape == (48, 64, 3)
    # Quality 75 keeps a flat color within a few levels
    assert np.abs(saved - (40, 160, 200)).mean() < 3


def red(pixels):
    """Which pixels are red."""
    r, g, b = pixels[..., 0], pixels[..., 1], pixels[..., 2]
    return (r > 150) & (g < 100) & (b < 100)


def test_a_detection_is_drawn_as_a_labelled_box(tmp_path):
    # A 400x400 frame, with the box from (100, 200) to (300, 360)
    save(tmp_path, make_frame(400, 400), result=make_result((0.5, 0.25, 0.9, 0.75)))
    found = red(read(tmp_path / "0.jpg"))
    # The box's sides, bottom and top are drawn
    assert found[280, 101] and found[280, 299] and found[359, 200] and found[201, 200]
    # Its inside and the frame's corners are not
    assert not found[280, 200].any() and not found[10, 10] and not found[390, 390]
    # The label is a band above the box's top left corner
    assert found[185:199, 101:120].mean() > 0.5
    assert not found[185:199, 280:299].any()


def test_a_box_at_the_top_of_the_frame_has_its_label_inside(tmp_path):
    save(tmp_path, make_frame(400, 400), result=make_result((0.0, 0.25, 0.5, 0.75)))
    found = red(read(tmp_path / "0.jpg"))
    assert found[3:15, 103:120].mean() > 0.5


def test_each_frame_is_named_by_its_seq(tmp_path):
    save(tmp_path, make_frame(), make_frame(), first_seq=41)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["41.jpg", "42.jpg"]


def test_the_directory_is_made_if_missing(tmp_path):
    save(tmp_path / "a" / "b", make_frame())
    assert (tmp_path / "a" / "b" / "0.jpg").is_file()


def test_a_frame_that_is_not_rgb_is_not_saved_and_later_frames_still_are(tmp_path):
    bad = make_frame()
    bad.channels = 4
    save(tmp_path, bad, make_frame())
    assert [p.name for p in tmp_path.iterdir()] == ["1.jpg"]


def test_frames_past_a_full_queue_are_skipped_and_counted(tmp_path):
    saver = FrameSaver(tmp_path, queue_size=1)
    # Hold the writer up so the queue fills
    blocker = make_frame(4000, 3000)
    accepted = [saver.submit(seq, blocker, make_result()) for seq in range(6)]
    assert not all(accepted)
    assert saver.skipped == accepted.count(False)
    saver.close()


def test_file_names_are_the_seq():
    assert frame_file_name(42) == "42.jpg"
