import shutil

from conftest import make_image

from led_clock.snapshot import JpegSaver


def test_a_snapshot_is_saved_after_its_directory_is_removed(tmp_path):
    directory = tmp_path / "snapshots"
    saver = JpegSaver(directory)
    saver.save("1.jpeg", make_image(0))
    shutil.rmtree(directory)
    saver.save("2.jpeg", make_image(0))
    assert (directory / "2.jpeg").is_file()
