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


def test_a_question_replaces_the_default_one():
    for reference, n_images in ((None, 1), (b"reference", 2)):
        model = Model("yes")
        verifier = Verifier(model, label="a dog", reference=reference, question="Is {prompt} here? Answer yes or no.")
        keep, _ = verifier.on_input(make_input(1, make_frame()))
        question, images = model.asked[0]
        assert keep and question == "Is a dog here? Answer yes or no." and len(images) == n_images


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
    assert main.parse_verifier_args(json.dumps({"prompt": "a tarp"}))["question"] == ""
    args = main.parse_verifier_args(json.dumps({"image": "t.jpg", "question": " Same kind of animal? "}))
    assert args["question"] == "Same kind of animal?"
    for bad in [{}, {"prompt": "p", "timeout_ms": 0}, {"prompt": "p", "model": ""}, {"prompt": "p", "log_tiles": 1},
                {"prompt": "p", "question": "Is {thing} here?"}, {"prompt": "p", "question": 3}]:
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


def test_the_box_image_question_shows_the_reference_then_the_crop():
    from cliped.verifier import BOX_IMAGE_QUESTION

    model = Model("yes")
    verifier = Verifier(model, label="person.jpg", reference=b"reference", question=BOX_IMAGE_QUESTION)
    keep, _ = verifier.on_input(make_input(1, make_frame()))
    question, images = model.asked[0]
    assert keep and question == BOX_IMAGE_QUESTION and images[0] == b"reference" and len(images) == 2
    assert "red box" in question and "{" not in question


# The live directory and the prompt heartbeat

import os

from cliped.verifier import BOX_QUESTION, LiveDir, PromptKeeper, clean_description


def write(path, data, mtime):
    """Writes `data` to `path` with the modification time `mtime` (seconds),
    so a change is seen whatever the filesystem's timestamp resolution."""
    path.write_bytes(data) if isinstance(data, bytes) else path.write_text(data)
    os.utime(path, (mtime, mtime))


def keeper_for(tmp_path, *, label="a tarp", describe=None, **kwargs):
    verifier = Verifier(Model("yes"), label=label, **kwargs)
    described = []

    def fake_describe(image):
        described.append(image)
        return describe(image) if describe else '"A Red Tarp."'

    keeper = PromptKeeper(verifier, live=LiveDir(tmp_path), describe=fake_describe,
                          load_image=lambda path: b"encoded " + path.read_bytes(), interval_s=1.0)
    return verifier, keeper, described


def test_without_a_live_directory_the_heartbeat_mirrors_a_text_prompt_every_interval():
    keeper = PromptKeeper(Verifier(Model("yes"), label="a red tarp"), interval_s=1.0)
    assert keeper.step(0.0) == "a red tarp".encode()
    assert keeper.step(0.5) is None
    assert keeper.step(1.0) == b"a red tarp"


def test_an_image_prompt_is_described_once_for_the_heartbeat(tmp_path):
    verifier, keeper, described = keeper_for(tmp_path, label="tarp.jpg", reference=b"reference")
    assert keeper.step(0.0) == b"A Red Tarp"
    assert keeper.step(1.0) == b"A Red Tarp"
    assert described == [b"reference"]


def test_a_failed_description_is_asked_again_at_the_next_heartbeat(tmp_path):
    answers = iter([RuntimeError("Ollama is down"), "a red tarp"])

    def describe(image):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    verifier, keeper, _ = keeper_for(tmp_path, label="tarp.jpg", reference=b"reference", describe=describe)
    assert keeper.step(0.0) is None
    assert keeper.step(1.0) == b"a red tarp"


def test_the_live_directory_wins_over_the_args_when_it_starts(tmp_path):
    write(tmp_path / "question.txt", "box\n", 100)
    write(tmp_path / "prompt.txt", " a blue tent \n", 100)
    verifier, keeper, _ = keeper_for(tmp_path)
    assert keeper.update()
    assert verifier.label == "a blue tent" and verifier.reference is None
    assert verifier.question(b"png")[0] == BOX_QUESTION.format(prompt="a blue tent")
    assert keeper.step(0.0) == b"a blue tent"


def test_an_image_wins_over_text_and_changes_are_followed(tmp_path):
    write(tmp_path / "prompt.txt", "a blue tent", 100)
    verifier, keeper, described = keeper_for(tmp_path)
    keeper.update()
    assert verifier.label == "a blue tent"
    write(tmp_path / "prompt.jpg", b"jpeg 1", 200)
    assert keeper.step(0.0) == b"A Red Tarp"
    assert verifier.reference == b"encoded jpeg 1" and verifier.label == "prompt.jpg"
    # Unchanged: nothing is read or described again
    assert not keeper.update() and len(described) == 1
    write(tmp_path / "prompt.jpg", b"jpeg 2!", 300)
    keeper.update()
    assert verifier.reference == b"encoded jpeg 2!" and verifier.description is None
    # The newer of prompt.jpg and prompt.jpeg
    write(tmp_path / "prompt.jpeg", b"jpeg 3", 400)
    keeper.update()
    assert verifier.reference == b"encoded jpeg 3"


def test_the_question_changes_on_its_own(tmp_path):
    write(tmp_path / "prompt.txt", "a blue tent", 100)
    verifier, keeper, _ = keeper_for(tmp_path)
    keeper.update()
    write(tmp_path / "question.txt", "Is there {prompt} here? yes or no", 100)
    assert keeper.update()
    assert verifier.label == "a blue tent"
    assert verifier.question(b"png")[0] == "Is there a blue tent here? yes or no"
    # A bad question is ignored, the last kept
    write(tmp_path / "question.txt", "Is there {target}?", 200)
    assert not keeper.update()
    assert verifier.question(b"png")[0] == "Is there a blue tent here? yes or no"


def test_removing_the_files_or_the_directory_keeps_the_last_prompt_and_question(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    write(live / "prompt.txt", "a blue tent", 100)
    write(live / "question.txt", "Is it {prompt}? yes or no", 100)
    verifier, keeper, _ = keeper_for(live)
    keeper.update()
    (live / "prompt.txt").unlink()
    (live / "question.txt").unlink()
    assert not keeper.update()
    live.rmdir()
    assert not keeper.update()
    assert verifier.label == "a blue tent" and verifier.custom_question == "Is it {prompt}? yes or no"
    # And a directory that comes back later is read
    live.mkdir()
    write(live / "prompt.txt", "a green car", 500)
    assert keeper.update() and verifier.label == "a green car"


def test_an_image_that_cant_be_read_is_tried_again(tmp_path):
    write(tmp_path / "prompt.jpg", b"partial", 100)
    tries = []

    def load(path):
        tries.append(path)
        if len(tries) == 1:
            raise ValueError("not an image yet")
        return b"encoded"

    verifier = Verifier(Model("yes"), label="a tarp")
    keeper = PromptKeeper(verifier, live=LiveDir(tmp_path), describe=lambda image: "a tarp", load_image=load)
    assert not keeper.update() and verifier.reference is None
    assert keeper.update() and verifier.reference == b"encoded"


def test_clean_description():
    assert clean_description('  "A person in a\n red shirt."  ') == "A person in a red shirt"


def test_live_and_heartbeat_args():
    args = main.parse_verifier_args(json.dumps({"prompt": "a tarp"}))
    assert args["live"] == "" and args["heartbeat"] == 1.0
    # With a live directory, the prompt may come from it instead
    args = main.parse_verifier_args(json.dumps({"live": "mission1", "heartbeat": 0.5}))
    assert args["prompt"] == "" and args["heartbeat"] == 0.5
    with pytest.raises(main.ConfigError):
        main.parse_verifier_args(json.dumps({}))
    for bad in ({"heartbeat": 0}, {"heartbeat": "1"}, {"live": 3}):
        with pytest.raises(main.ConfigError):
            main.parse_verifier_args(json.dumps({"prompt": "a tarp", **bad}))


def test_the_live_directory_must_be_inside_the_files_directory(tmp_path):
    from cliped.files import resolve_dir

    assert resolve_dir(tmp_path, "live") == (tmp_path / "live").resolve()
    for bad in ("../elsewhere", "/abs", "", "."):
        with pytest.raises(FilesError):
            resolve_dir(tmp_path, bad)
