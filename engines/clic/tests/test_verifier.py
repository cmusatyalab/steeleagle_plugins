import json

import cv2
import numpy as np
import pytest
from conftest import make_frame, make_input

from clic import main
from clic.files import FilesError, TileLog, resolve
from clic.verifier import (CATEGORY_QUESTION, CONTRASTIVE_QUESTION, DESCRIPTIVE_QUESTION, MAX_DISTRACTORS, Verifier,
                             parse_answer)
from steeleagle_protocol.v1.messages.result import result_pb2


class Model:
    """Stands in for Ollama: answers `answer`, and records what it was asked.
    Asked with scores, P(yes) is 0.95 for an answer starting with yes and
    0.05 for any other, and P(no) the rest."""

    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def __call__(self, question, images, max_tokens=8, scores=False):
        self.asked.append((question, images))
        answer = self.answer(question) if callable(self.answer) else self.answer
        if not scores:
            return answer
        p_yes = 0.95 if answer.strip().lower().startswith("yes") else 0.05
        return answer, p_yes, round(1 - p_yes, 4)


def verdict(result):
    return json.loads(result_pb2.GenericResult.FromString(result).payload)


def test_a_match_is_forwarded_with_a_yes():
    model = Model("Yes.")
    keep, result = Verifier(model, label="a white square").on_input(make_input(1, make_frame(id=5)))
    assert keep and verdict(result) == {"match": True, "p_yes": 0.95, "p_no": 0.05}
    question, images = model.asked[0]
    assert "a white square" in question and len(images) == 1
    assert cv2.imdecode(np.frombuffer(images[0], np.uint8), cv2.IMREAD_COLOR).shape == (300, 400, 3)


def test_anything_but_yes_is_dropped_with_a_no():
    for answer in ["No", "no.", "maybe"]:
        keep, result = Verifier(Model(answer), label="x").on_input(make_input(1, make_frame()))
        assert not keep and verdict(result) == {"match": False, "p_yes": 0.05, "p_no": 0.95}


def named(text, answer="yes"):
    """A model that gives `text` for an image and `answer` about a frame."""
    describe = (CATEGORY_QUESTION, DESCRIPTIVE_QUESTION, *(CONTRASTIVE_QUESTION.format(n=n) for n in range(1, 9)))
    return Model(lambda q: text if q in describe else answer)


def described(model):
    """What the model was asked about an image, but its kind: the
    description, asked after the kind in every mode but "class"."""
    return [asked for asked in model.asked if asked[0] != CATEGORY_QUESTION]


def heartbeat(payload):
    """The heartbeat's payload as (class, description), None for none."""
    if payload is None:
        return None
    data = json.loads(payload)
    return data["class"], data["description"]


def text_of(payload):
    """The text CLIP ranks by in the heartbeat's payload: its description,
    else its class."""
    cls, description = heartbeat(payload)
    return description or cls


def test_an_image_prompt_becomes_its_kind_and_the_model_sees_only_the_frame():
    model = named("'A quadruped robot dog.'")
    verifier = Verifier(model, label="dog.jpg", reference=b"reference")
    # Named as soon as it is set, before any frame
    assert verifier.description == "A quadruped robot dog" and model.asked == [(CATEGORY_QUESTION, [b"reference"])]
    for _ in range(2):
        keep, result = verifier.on_input(make_input(1, make_frame()))
        assert keep and verdict(result)["match"] is True
    question = "This is a tile cut from an aerial drone photo. Does it show A quadruped robot dog? " \
               "Answer with only yes or no."
    assert [q for q, _ in model.asked[1:]] == [question, question]
    assert all(len(images) == 1 and images[0] != b"reference" for _, images in model.asked[1:])


def test_image_mode_descriptive_describes_the_image_instead():
    model = named("Mannequin in dark green shirt and black pants.")
    verifier = Verifier(model, label="m.jpg", reference=b"reference", image_mode="descriptive")
    assert model.asked == [(CATEGORY_QUESTION, [b"reference"]), (DESCRIPTIVE_QUESTION, [b"reference"])]
    assert verifier.description == "Mannequin in dark green shirt and black pants"


def test_contrastive_shows_the_distractors_after_the_image():
    model = named("Person in black hoodie and blue jeans")
    verifier = Verifier(model, label="p.jpg", reference=b"target", image_mode="contrastive",
                        distractors=[("a.jpg", b"gray"), ("b.jpg", b"white")])
    assert described(model) == [(CONTRASTIVE_QUESTION.format(n=2), [b"target", b"gray", b"white"])]
    assert verifier.description == "Person in black hoodie and blue jeans"


def test_contrastive_without_distractors_describes_then_asks_again_when_they_come():
    model = named("Person in dark jacket")
    verifier = Verifier(model, label="p.jpg", reference=b"target", image_mode="contrastive")
    assert described(model) == [(DESCRIPTIVE_QUESTION, [b"target"])]
    verifier.set_live_distractors([("d1.jpg", b"gray")])
    assert verifier.description is None
    verifier.describe_image()
    assert model.asked[-1] == (CONTRASTIVE_QUESTION.format(n=1), [b"target", b"gray"])
    # Its kind is asked once: distractors change only the description
    assert sum(q == CATEGORY_QUESTION for q, _ in model.asked) == 1


def test_the_contrastive_question_has_no_negation_to_pass_on():
    assert "does not understand negation" in CONTRASTIVE_QUESTION
    assert CONTRASTIVE_QUESTION.format(n=3).count("3 image") == 1


def test_only_the_newest_distractors_are_shown():
    model = named("x")
    many = [(f"d{i}.jpg", bytes([i])) for i in range(MAX_DISTRACTORS + 3)]
    Verifier(model, label="p.jpg", reference=b"t", image_mode="contrastive", distractors=many)
    assert described(model)[0][1] == [b"t", *[image for _, image in many[-MAX_DISTRACTORS:]]]


def test_class_and_descriptive_ignore_distractors():
    for mode, questions in (("class", [CATEGORY_QUESTION]), ("descriptive", [CATEGORY_QUESTION, DESCRIPTIVE_QUESTION])):
        model = named("x")
        verifier = Verifier(model, label="p.jpg", reference=b"t", image_mode=mode, distractors=[("a", b"a")])
        verifier.set_live_distractors([("d1.jpg", b"b")])
        assert model.asked == [(q, [b"t"]) for q in questions] and verifier.description == "x"


def test_a_bad_image_mode_is_refused():
    with pytest.raises(ValueError):
        Verifier(Model("yes"), label="a dog", image_mode="strict")


def test_a_text_prompt_is_used_as_is_either_way():
    for mode in ("class", "descriptive", "contrastive"):
        model = Model("yes")
        verifier = Verifier(model, label="a dog", question="box", image_mode=mode)
        verifier.on_input(make_input(1, make_frame()))
        assert [q for q, _ in model.asked] == [BOX_QUESTION.format(prompt="a dog")]


def test_a_question_replaces_the_default_one():
    for reference in (None, b"reference"):
        model = named("a dog")
        verifier = Verifier(model, label="a dog", reference=reference, question="Is {prompt} here? Answer yes or no.")
        keep, _ = verifier.on_input(make_input(1, make_frame()))
        question, images = model.asked[-1]
        assert keep and question == "Is a dog here? Answer yes or no." and len(images) == 1


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
    assert (args["model"], args["timeout_ms"], args["log_tiles"]) == ("qwen3-vl:30b-a3b-instruct", 30000, False)
    args = main.parse_verifier_args(json.dumps({"prompt": "a tarp", "image": "t.jpg"}))
    assert (args["prompt"], args["image"]) == ("", "t.jpg")
    assert main.parse_verifier_args(json.dumps({"prompt": "a tarp"}))["question"] == ""
    args = main.parse_verifier_args(json.dumps({"image": "t.jpg", "question": " Same kind of animal? "}))
    assert args["question"] == "Same kind of animal?"
    assert args["image_mode"] == "class" and (args["yes_conf"], args["no_conf"]) == (0.7, 0.95)
    assert args["distractors"] == []
    args = main.parse_verifier_args(json.dumps({"image": "t.jpg", "image_mode": "contrastive",
                                                "distractors": [" a.jpg ", "b.jpg"]}))
    assert args["image_mode"] == "contrastive" and args["distractors"] == ["a.jpg", "b.jpg"]
    for bad in [{}, {"prompt": "p", "timeout_ms": 0}, {"prompt": "p", "model": ""}, {"prompt": "p", "log_tiles": 1},
                {"prompt": "p", "question": "Is {thing} here?"}, {"prompt": "p", "question": 3},
                {"prompt": "p", "image_mode": "descriptive_strict"}, {"prompt": "p", "image_mode": True},
                {"prompt": "p", "distractors": "a.jpg"}, {"prompt": "p", "distractors": [""]},
                {"prompt": "p", "yes_conf": 0.5},
                {"prompt": "p", "no_conf": 1.1}, {"prompt": "p", "yes_conf": True}, {"prompt": "p", "no_conf": "0.9"}]:
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

    from clic.verifier import REFERENCE_MAX_SIDE, shrink

    assert shrink(np.zeros((4284, 5712, 3), np.uint8)).shape == (336, REFERENCE_MAX_SIDE, 3)
    assert shrink(np.zeros((5712, 4284, 3), np.uint8)).shape == (REFERENCE_MAX_SIDE, 336, 3)
    small = np.zeros((100, 200, 3), np.uint8)
    assert shrink(small) is small


# The live directory and the prompt heartbeat

import os

from clic.verifier import BOX_QUESTION, LiveDir, PromptKeeper, clean_description


def write(path, data, mtime):
    """Writes `data` to `path` with the modification time `mtime` (seconds),
    so a change is seen whatever the filesystem's timestamp resolution."""
    path.write_bytes(data) if isinstance(data, bytes) else path.write_text(data)
    os.utime(path, (mtime, mtime))


def keeper_for(tmp_path, *, label="a tarp", describe=None, **kwargs):
    """A verifier whose model names any image with `describe(image)` (by
    default '"A Red Tarp."'), its PromptKeeper over the live directory
    `tmp_path`, and the images it was asked to name."""
    described = []

    def ask(question, images, max_tokens=8, scores=False):
        if question in (CATEGORY_QUESTION, DESCRIPTIVE_QUESTION):
            described.append(images[0])
            return describe(images[0]) if describe else '"A Red Tarp."'
        return ("yes", 0.95, 0.05) if scores else "yes"

    verifier = Verifier(ask, label=label, **kwargs)
    keeper = PromptKeeper(verifier, live=LiveDir(tmp_path), load_image=lambda path: b"encoded " + path.read_bytes(),
                          interval_s=1.0)
    return verifier, keeper, described


def test_without_a_live_directory_the_heartbeat_mirrors_a_text_prompt_every_interval():
    keeper = PromptKeeper(Verifier(Model("yes"), label="a red tarp"), interval_s=1.0)
    assert heartbeat(keeper.step(0.0)) == (None, "a red tarp")
    assert keeper.step(0.5) is None
    assert heartbeat(keeper.step(1.0)) == (None, "a red tarp")


def test_an_image_prompt_is_described_once_for_the_heartbeat(tmp_path):
    verifier, keeper, described = keeper_for(tmp_path, label="tarp.jpg", reference=b"reference")
    # In "class" mode the kind is its text: no description for CLIP
    assert heartbeat(keeper.step(0.0)) == ("A Red Tarp", None)
    assert heartbeat(keeper.step(1.0)) == ("A Red Tarp", None)
    assert described == [b"reference"]


def test_a_failed_description_is_asked_again_at_the_next_heartbeat(tmp_path):
    answers = iter([RuntimeError("Ollama is down"), RuntimeError("still down"), "a red tarp"])

    def describe(image):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    # At startup, then at the first heartbeat, the model is down
    verifier, keeper, _ = keeper_for(tmp_path, label="tarp.jpg", reference=b"reference", describe=describe)
    assert keeper.step(0.0) is None
    assert text_of(keeper.step(1.0)) == "a red tarp"


def test_the_live_directory_wins_over_the_args_when_it_starts(tmp_path):
    write(tmp_path / "question.txt", "box\n", 100)
    write(tmp_path / "prompt.txt", " a blue tent \n", 100)
    verifier, keeper, _ = keeper_for(tmp_path)
    assert keeper.update()
    assert verifier.label == "a blue tent" and verifier.reference is None
    assert verifier.question(b"png")[0] == BOX_QUESTION.format(prompt="a blue tent")
    assert heartbeat(keeper.step(0.0)) == (None, "a blue tent")


def test_an_image_wins_over_text_and_changes_are_followed(tmp_path):
    write(tmp_path / "prompt.txt", "a blue tent", 100)
    verifier, keeper, described = keeper_for(tmp_path)
    keeper.update()
    assert verifier.label == "a blue tent"
    write(tmp_path / "prompt.jpg", b"jpeg 1", 200)
    assert text_of(keeper.step(0.0)) == "A Red Tarp"
    assert verifier.reference == b"encoded jpeg 1" and verifier.label == "prompt.jpg"
    # Unchanged: nothing is read or described again
    assert not keeper.update() and len(described) == 1
    write(tmp_path / "prompt.jpg", b"jpeg 2!", 300)
    keeper.update()
    assert verifier.reference == b"encoded jpeg 2!" and len(described) == 2
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

    verifier = Verifier(named("a tarp"), label="a tarp")
    keeper = PromptKeeper(verifier, live=LiveDir(tmp_path), load_image=load)
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
    from clic.files import resolve_dir

    assert resolve_dir(tmp_path, "live") == (tmp_path / "live").resolve()
    for bad in ("../elsewhere", "/abs", "", "."):
        with pytest.raises(FilesError):
            resolve_dir(tmp_path, bad)


def test_the_model_is_loaded_with_a_small_context(monkeypatch):
    from clic.verifier import Ollama

    sent = []

    def post(self, path, body, timeout_s):
        sent.append(body)
        return {"message": {"content": "yes"}}

    monkeypatch.setattr(Ollama, "_post", post)
    Ollama("localhost:11434", "m").ask("q?", [b"png"])
    Ollama("localhost:11434", "m", num_ctx=0).ask("q?", [b"png"])
    assert sent[0]["options"]["num_ctx"] == 4096
    assert "num_ctx" not in sent[1]["options"]  # Ollama's default
    assert main.parse_verifier_args(json.dumps({"prompt": "p"}))["num_ctx"] == 4096
    for bad in (-1, 1.5, "4096"):
        with pytest.raises(main.ConfigError):
            main.parse_verifier_args(json.dumps({"prompt": "p", "num_ctx": bad}))


def test_a_new_image_is_named_when_it_is_set():
    model = named("A quadruped robot dog")
    verifier = Verifier(model, label="dog.jpg", reference=b"reference", question="box")
    verifier.on_input(make_input(1, make_frame()))
    verifier.set_prompt("other.jpg", b"other")
    assert model.asked[-1] == (CATEGORY_QUESTION, [b"other"]) and verifier.description == "A quadruped robot dog"
    # A text prompt replaces it as is
    verifier.set_prompt("a cat")
    assert verifier.description == "a cat" and len(model.asked) == 3


def test_an_image_the_model_couldnt_name_is_named_at_the_next_frame():
    down = [True]

    def answer(question):
        if question == CATEGORY_QUESTION:
            if down[0]:
                raise RuntimeError("Ollama is down")
            return "a quadruped robot dog"
        return "yes"
    model = Model(answer)
    verifier = Verifier(model, label="dog.jpg", reference=b"reference", question="box")
    assert verifier.description is None
    down[0] = False
    keep, _ = verifier.on_input(make_input(1, make_frame()))
    assert keep and verifier.description == "a quadruped robot dog"
    assert [q for q, _ in model.asked][:2] == [CATEGORY_QUESTION, CATEGORY_QUESTION]


def test_a_live_image_is_described_when_it_changes(tmp_path):
    model = named("A gray robotic dog")
    verifier = Verifier(model, label="a dog", question="box", image_mode="descriptive")
    live = tmp_path / "live"
    live.mkdir()
    keeper = PromptKeeper(verifier, live=LiveDir(live), load_image=lambda path: path.read_bytes())
    (live / "prompt.jpg").write_bytes(b"photo")
    keeper.update()
    assert described(model) == [(DESCRIPTIVE_QUESTION, [b"photo"])] and verifier.description == "A gray robotic dog"
    assert heartbeat(keeper.step(0.0)) == ("A gray robotic dog", "A gray robotic dog")


def test_a_class_written_with_the_image_is_its_kind(tmp_path):
    model = named("Person in black hoodie and blue jeans")
    verifier = Verifier(model, label="moving", question="box", image_mode="descriptive")
    keeper = PromptKeeper(verifier, live=LiveDir(tmp_path), load_image=lambda path: path.read_bytes())
    (tmp_path / "class.txt").write_text("person\n")
    (tmp_path / "prompt.jpg").write_bytes(b"target")
    assert heartbeat(keeper.step(0.0)) == ("a person", "Person in black hoodie and blue jeans")
    # Not asked the kind: only the description
    assert [q for q, _ in model.asked] == [DESCRIPTIVE_QUESTION]


def test_a_stale_class_file_is_not_the_kind(tmp_path):
    model = named("a person")
    verifier = Verifier(model, label="moving", question="box", image_mode="descriptive")
    keeper = PromptKeeper(verifier, live=LiveDir(tmp_path), load_image=lambda path: path.read_bytes())
    write(tmp_path / "class.txt", "robotic dog", 100)
    (tmp_path / "prompt.jpg").write_bytes(b"target")
    keeper.step(0.0)
    assert verifier.kind == "a person" and model.asked[0][0] == CATEGORY_QUESTION


def test_the_heartbeat_holds_the_kind_and_the_description():
    def answer(question):
        return "a person" if question == CATEGORY_QUESTION else \
            "Person in black hoodie and blue jeans" if question == DESCRIPTIVE_QUESTION else "yes"
    for mode, expected in (("class", ("a person", None)),
                           ("descriptive", ("a person", "Person in black hoodie and blue jeans")),
                           ("contrastive", ("a person", "Person in black hoodie and blue jeans"))):
        keeper = PromptKeeper(Verifier(Model(answer), label="p.jpg", reference=b"t", image_mode=mode))
        assert heartbeat(keeper.step(0.0)) == expected, mode


def test_the_text_prompt_is_written_to_the_live_directory(tmp_path):
    model = named("Person in black hoodie and blue jeans")
    verifier = Verifier(model, label="a person", question="box", image_mode="descriptive")
    keeper = PromptKeeper(verifier, live=LiveDir(tmp_path), load_image=lambda path: path.read_bytes())
    keeper.step(0.0)
    assert (tmp_path / "description.txt").read_text() == "a person\n"
    (tmp_path / "prompt.jpg").write_bytes(b"target")
    keeper.step(5.0)
    assert (tmp_path / "description.txt").read_text() == "Person in black hoodie and blue jeans\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["description.txt", "prompt.jpg"]


def test_live_distractors_are_read_and_the_image_described_again(tmp_path):
    model = named("Person in black hoodie and blue jeans")
    verifier = Verifier(model, label="a person", question="box", image_mode="contrastive")
    keeper = PromptKeeper(verifier, live=LiveDir(tmp_path), load_image=lambda path: path.read_bytes())
    (tmp_path / "prompt.jpg").write_bytes(b"target")
    keeper.update()
    assert described(model) == [(DESCRIPTIVE_QUESTION, [b"target"])]
    (tmp_path / "d2.jpg").write_bytes(b"white")
    (tmp_path / "d1.jpg").write_bytes(b"gray")
    (tmp_path / "decoy.jpg").write_bytes(b"not a distractor")
    assert keeper.update()
    keeper.step(0.0)
    question, images = model.asked[-1]
    assert question == CONTRASTIVE_QUESTION.format(n=2) and images[0] == b"target"
    assert sorted(images[1:]) == [b"gray", b"white"]
    asked = len(model.asked)
    assert not keeper.update() and keeper.step(5.0) is not None and len(model.asked) == asked


class ScoredModel:
    """Stands in for Ollama asked with scores: answers `answer` with the
    probabilities `p_yes` and `p_no` (None for a server without them)."""

    def __init__(self, answer, p_yes, p_no):
        self.answer, self.p_yes, self.p_no = answer, p_yes, p_no
        self.scored = []

    def __call__(self, question, images, max_tokens=8, scores=False):
        self.scored.append(scores)
        return (self.answer, self.p_yes, self.p_no) if scores else self.answer


def confident(model):
    return Verifier(model, label="a mannequin")


def test_with_confidences_the_probabilities_decide_and_are_reported():
    keep, result = confident(ScoredModel("Yes", 0.8, 0.2)).on_input(make_input(1, make_frame()))
    assert keep and verdict(result) == {"match": True, "p_yes": 0.8, "p_no": 0.2}
    keep, result = confident(ScoredModel("No", 0.05, 0.95)).on_input(make_input(1, make_frame()))
    assert not keep and verdict(result) == {"match": False, "p_yes": 0.05, "p_no": 0.95}


def test_between_the_confidences_a_frame_is_forwarded_as_unsure():
    # The model's word is no, but not confidently: forwarded, not to be learned
    for p_yes in (0.11, 0.5, 0.69):
        model = ScoredModel("No", p_yes, 1 - p_yes)
        v = confident(model)
        keep, result = v.on_input(make_input(1, make_frame()))
        assert keep and model.scored == [True]
        assert verdict(result) == {"match": None, "unsure": True, "p_yes": p_yes, "p_no": round(1 - p_yes, 4)}
        assert (v.matched, v.unsure, v.frames) == (0, 1, 1)


def test_without_probabilities_the_answer_decides():
    keep, result = confident(ScoredModel("No", None, None)).on_input(make_input(1, make_frame()))
    assert not keep and verdict(result) == {"match": False}


def test_the_confidences_can_be_set():
    keep, result = Verifier(ScoredModel("Yes", 0.8, 0.2), label="x", yes_conf=0.85).on_input(make_input(1, make_frame()))
    assert keep and verdict(result)["unsure"] is True
    keep, result = Verifier(ScoredModel("No", 0.15, 0.85), label="x", no_conf=0.8).on_input(make_input(1, make_frame()))
    assert not keep and verdict(result)["match"] is False
    for kwargs in ({"yes_conf": 0.5}, {"no_conf": 1.2}, {"yes_conf": True}):
        with pytest.raises(ValueError):
            Verifier(Model("yes"), label="x", **kwargs)


def test_answer_scores_sums_the_spellings_of_yes_and_no():
    import math

    from clic.verifier import answer_scores

    top = [{"token": "No", "logprob": math.log(0.6)}, {"token": " no", "logprob": math.log(0.1)},
           {"token": "Yes", "logprob": math.log(0.25)}, {"token": "YES", "logprob": math.log(0.04)},
           {"token": "Maybe", "logprob": math.log(0.01)}]
    p_yes, p_no = answer_scores([{"token": "No", "logprob": math.log(0.6), "top_logprobs": top}])
    assert (round(p_yes, 6), round(p_no, 6)) == (0.29, 0.7)
    assert answer_scores(None) == (None, None) and answer_scores([]) == (None, None)


def test_asking_for_scores_asks_ollama_for_logprobs(monkeypatch):
    import math

    from clic.verifier import Ollama

    sent = []

    def post(self, path, body, timeout_s):
        sent.append(body)
        return {"message": {"content": "Yes"},
                "logprobs": [{"token": "Yes", "logprob": math.log(0.9),
                              "top_logprobs": [{"token": "Yes", "logprob": math.log(0.9)},
                                               {"token": "No", "logprob": math.log(0.1)}]}]}

    monkeypatch.setattr(Ollama, "_post", post)
    answer, p_yes, p_no = Ollama("localhost:11434", "m").ask("q?", [b"png"], scores=True)
    assert answer == "Yes" and round(p_yes, 6) == 0.9 and round(p_no, 6) == 0.1
    assert sent[0]["logprobs"] is True and sent[0]["top_logprobs"] == 20
    assert Ollama("localhost:11434", "m").ask("q?", [b"png"]) == "Yes" and "logprobs" not in sent[1]
