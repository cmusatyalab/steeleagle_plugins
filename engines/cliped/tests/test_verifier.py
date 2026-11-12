import json

import cv2
import numpy as np
import pytest
from conftest import make_frame, make_input

from cliped import main
from cliped.files import FilesError, TileLog, resolve
from cliped.verifier import IMAGE_QUESTION, Verifier, parse_answer
from steeleagle_protocol.v1.messages.result import result_pb2


class Model:
    """Stands in for Ollama: answers `answer`, and records what it was asked."""

    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def __call__(self, question, images):
        self.asked.append((question, images))
        return self.answer


def verdict(result):
    return json.loads(result_pb2.GenericResult.FromString(result).payload)


def test_a_match_is_forwarded_with_a_yes():
    model = Model("Yes.")
    keep, result = Verifier(model, label="a white square").on_input(make_input(1, make_frame(id=5)))
    assert keep and verdict(result) == {"match": True}
    question, images = model.asked[0]
    assert "a white square" in question and len(images) == 1
    assert cv2.imdecode(np.frombuffer(images[0], np.uint8), cv2.IMREAD_COLOR).shape == (300, 400, 3)


def test_anything_but_yes_is_dropped_with_a_no():
    for answer in ["No", "no.", "maybe"]:
        keep, result = Verifier(Model(answer), label="x").on_input(make_input(1, make_frame()))
        assert not keep and verdict(result) == {"match": False}


def test_an_image_prompt_shows_the_reference_first():
    model = Model("yes")
    keep, result = Verifier(model, label="tarp.jpg", reference=b"reference").on_input(make_input(1, make_frame()))
    question, images = model.asked[0]
    assert question == IMAGE_QUESTION and images[0] == b"reference" and len(images) == 2
    assert keep and verdict(result) == {"match": True}


def test_parse_answer():
    assert parse_answer(" **Yes**") is True
    assert parse_answer("No, it does not") is False
    assert parse_answer("I can't tell") is None


def test_log_tiles_writes_inputs_and_the_frames_forwarded(tmp_path):
    log = TileLog(tmp_path / "verifier")
    Verifier(Model("yes"), label="x", tile_log=log).on_input(make_input(1, make_frame(id=7)))
    Verifier(Model("no"), label="x", tile_log=log).on_input(make_input(2, make_frame(id=8)))
    assert sorted(p.name for p in log.in_dir.iterdir()) == ["7.jpeg", "8.jpeg"]
    assert [p.name for p in log.out_dir.iterdir()] == ["7_0.jpeg"]


def test_verifier_args():
    args = main.parse_verifier_args(json.dumps({"prompt": "a tarp"}))
    assert (args["model"], args["timeout_ms"], args["log_tiles"]) == ("qwen3-vl:8b-instruct", 30000, False)
    args = main.parse_verifier_args(json.dumps({"prompt": "a tarp", "image": "t.jpg"}))
    assert (args["prompt"], args["image"]) == ("", "t.jpg")
    for bad in [{}, {"prompt": "p", "timeout_ms": 0}, {"prompt": "p", "model": ""}, {"prompt": "p", "log_tiles": 1}]:
        with pytest.raises(main.ConfigError):
            main.parse_verifier_args(json.dumps(bad))


def test_an_image_must_be_inside_the_files_directory(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "t.jpg").write_bytes(b"x")
    assert resolve(tmp_path, "sub/t.jpg") == (tmp_path / "sub" / "t.jpg").resolve()
    for bad in ["", "/etc/passwd", "../t.jpg", "missing.jpg", "sub"]:
        with pytest.raises(FilesError):
            resolve(tmp_path, bad)


def test_shrink_caps_the_longer_side_and_keeps_the_aspect_ratio():
    import numpy as np

    from cliped.verifier import REFERENCE_MAX_SIDE, shrink

    assert shrink(np.zeros((4284, 5712, 3), np.uint8)).shape == (336, REFERENCE_MAX_SIDE, 3)
    assert shrink(np.zeros((5712, 4284, 3), np.uint8)).shape == (REFERENCE_MAX_SIDE, 336, 3)
    small = np.zeros((100, 200, 3), np.uint8)
    assert shrink(small) is small
