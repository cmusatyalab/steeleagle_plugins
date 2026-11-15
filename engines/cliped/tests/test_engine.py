import dataclasses
import json

import cv2
import numpy as np
import pytest
import uriel
from conftest import VERIFIER, BrightnessEncoder, input_id, make_event, make_frame, make_input

from cliped import main
from cliped.clip import QUANTIZED_MODEL, NpuImageEncoder, Selector, image_vector, tile_boxes
from cliped.engine import Cliped, FrameError, Reintegration, max_output_size
from cliped.files import TileLog
from cliped.send_queue import SendQueue, Tile
from steeleagle_protocol.v1.messages.frame import frame_pb2
from steeleagle_protocol.v1.messages.result import result_pb2

K = uriel.EventKind


@pytest.fixture
def cliped():
    encoder = BrightnessEncoder()
    return Cliped(
        encoder,
        Selector(encoder.t, encoder.b, margin=0.25, max_send=1),
        SendQueue(max_size=8, resend_after_s=5.0),
        prompt="a white square",
        verifier=VERIFIER,
        tile_size=200,
        stride=150,
    )


def test_tiles_cover_the_frame_edge_to_edge():
    boxes = tile_boxes(1280, 720, 200, 150)
    xs = sorted({b[0] for b in boxes})
    ys = sorted({b[1] for b in boxes})
    assert xs[0] == 0 and xs[-1] == 1080 and ys == [0, 150, 300, 450, 520]
    assert len(boxes) == len(xs) * len(ys) == 9 * 5
    assert all(x1 - x0 == y1 - y0 == 200 for x0, y0, x1, y1 in boxes)
    # A tile larger than the frame shrinks to fit it
    assert tile_boxes(120, 100, 200, 150) == [(0, 0, 100, 100), (20, 0, 120, 100)]


def test_the_selector_sends_only_a_standout_tile():
    selector = Selector(np.array([1.0, 0.0]), np.array([[0.0, 1.0]]), k=2, margin=0.25, max_send=2)
    v = np.array([[0.0, 1.0]] * 5 + [[1.0, 0.0]])
    picked, s = selector.select(v)
    assert list(picked) == [5] and s[5] == 1.0
    # Nothing beats the backgrounds: nothing is sent
    picked, _ = selector.select(np.array([[0.0, 1.0]] * 6))
    assert len(picked) == 0


def test_an_input_is_replaced_with_its_own_tile_and_classified(cliped):
    tile, result = cliped.on_input(make_input(1, make_frame(bright=(150, 0, 350, 200), id=40)), now=0)
    assert tile is not None and tile.frame_id == 40 and tile.box == (150, 0, 350, 200)
    out = frame_pb2.RawFrame.FromString(tile.payload)
    assert (out.h_res, out.v_res, out.channels, out.id, out.camera_id) == (200, 200, 3, 40, 3)
    assert set(out.data) == {255}

    classification = result_pb2.ClassificationResult.FromString(result)
    assert getattr(classification, "class") == "a white square"
    assert classification.score == pytest.approx(tile.score)
    # It is queued, in flight as the input it replaced
    assert len(cliped.queue) == 1 and tile.carriers == [input_id(1)]

    # An empty frame is dropped with no result, even with a tile in the queue
    cliped.on_event(make_event(K.TIMED_OUT, 1))
    assert cliped.on_input(make_input(2, make_frame(id=41)), now=1) == (None, b"")
    # The verifier has it: gone
    cliped.on_event(make_event(K.RESULT, 1))
    assert len(cliped.queue) == 0 and cliped.queue.confirmed == 1


def test_an_image_prompt_classifies_as_the_file_name():
    encoder = BrightnessEncoder()
    cliped = Cliped(encoder, Selector(encoder.t, encoder.b, margin=0.25, max_send=1),
                    SendQueue(max_size=8, resend_after_s=5.0), prompt="tarp.jpg", verifier=VERIFIER,
                    tile_size=200, stride=150)
    _, result = cliped.on_input(make_input(1, make_frame(bright=(150, 0, 350, 200))), now=0)
    assert getattr(result_pb2.ClassificationResult.FromString(result), "class") == "tarp.jpg"


class Publisher:
    """Stands in for the reintegration producer: records each payload."""

    def __init__(self):
        self.payloads = []

    def __call__(self, payload):
        self.payloads.append(payload)
        return uriel.InputId(PRODUCER_NAME, 9, len(self.payloads))


PRODUCER_NAME = "cliped.reintegration"


def reintegration_event(kind, seq, engine="local.encoder"):
    event = make_event(kind, seq, engine=engine)
    return dataclasses.replace(event, id=uriel.InputId(PRODUCER_NAME, 9, seq))


def queued(cliped, *scores):
    tiles = [Tile(score=s, payload=f"tile {s}".encode(), frame_id=int(s * 100), box=(0, 0, 1, 1)) for s in scores]
    for t in tiles:
        cliped.queue.push(t)
    return tiles


def test_reintegration_sends_the_queue_best_first_one_admission_at_a_time(cliped):
    queued(cliped, 0.2, 0.9, 0.5)
    reintegration, publish = Reintegration(cliped.queue), Publisher()
    assert reintegration.step(0, publish).score == 0.9
    # Not admitted yet: publishing another would replace it at the client
    assert reintegration.step(0, publish) is None and len(publish.payloads) == 1
    # Admitted (an event at the encoder): the next goes
    reintegration.on_event(reintegration_event(K.ACCEPTED, 1))
    assert reintegration.step(0, publish).score == 0.5
    reintegration.on_event(reintegration_event(K.SENT, 2))
    assert reintegration.step(0, publish).score == 0.2
    reintegration.on_event(reintegration_event(K.SENT, 3))
    # All in flight: nothing to send until the verifier answers
    assert reintegration.step(0, publish) is None
    cliped.on_event(dataclasses.replace(make_event(K.RESULT), id=uriel.InputId(PRODUCER_NAME, 9, 1)))
    assert len(cliped.queue) == 2 and cliped.queue.confirmed == 1
    # The verifier timed out on 0.5: sent again
    cliped.on_event(dataclasses.replace(make_event(K.TIMED_OUT), id=uriel.InputId(PRODUCER_NAME, 9, 2)))
    assert reintegration.step(1, publish).score == 0.5


def test_a_better_tile_replaces_the_one_waiting_for_admission(cliped):
    low, = queued(cliped, 0.2)
    reintegration, publish = Reintegration(cliped.queue), Publisher()
    assert reintegration.step(0, publish) is low
    high, = queued(cliped, 0.9)
    assert reintegration.step(0, publish) is high
    # The client replaced the low tile, so it waits again, and goes next
    assert not low.in_flight
    reintegration.on_event(reintegration_event(K.ACCEPTED, 2))
    assert reintegration.step(0, publish) is low


def test_a_tile_never_admitted_is_sent_again_after_resend_after(cliped):
    tile, = queued(cliped, 0.9)
    reintegration, publish = Reintegration(cliped.queue), Publisher()
    assert reintegration.step(0, publish) is tile
    assert reintegration.step(4, publish) is None
    assert reintegration.step(5, publish) is tile and tile.carriers[-1].seq == 2


def test_reintegration_does_not_send_the_tile_an_input_carries(cliped):
    cliped.on_input(make_input(1, make_frame(bright=(150, 0, 350, 200), id=40)), now=0)
    reintegration, publish = Reintegration(cliped.queue), Publisher()
    assert reintegration.step(0, publish) is None
    # Skipped at the verifier: the producer sends it instead
    cliped.on_event(make_event(K.SKIPPED, 1))
    assert reintegration.step(1, publish).frame_id == 40


def test_events_of_other_engines_are_ignored(cliped):
    cliped.on_input(make_input(1, make_frame(bright=(0, 0, 200, 200))), now=0)
    cliped.on_event(make_event(K.RESULT, 1, engine="10.0.0.1.other"))
    assert len(cliped.queue) == 1


def test_a_frame_that_is_not_rgb_is_refused(cliped):
    frame = frame_pb2.RawFrame(data=b"\0" * 11, h_res=2, v_res=2, channels=3)
    with pytest.raises(FrameError):
        cliped.on_input(make_input(1, frame.SerializeToString()))


def test_the_output_fits_a_tile_and_its_result():
    assert max_output_size(200) > 200 * 200 * 3 + 1000


def test_args_default_and_validate():
    args = main.parse_args(json.dumps({"prompt": " a red tarp ", "verifier": "verifier"}))
    assert args["prompt"] == "a red tarp"
    assert (args["quantized"], args["tile_size"], args["stride"], args["margin"], args["max_send"]) == (
        True, 200, 150, 0.25, 1
    )
    assert args["backgrounds"] == ["grass", "dirt", "rocks", "trees", "a road", "a roof"]
    assert args["npu"] is True
    assert (args["log_scores"], args["log_tiles"], args["image"]) == (False, False, "")
    # An image wins over a prompt
    args = main.parse_args(json.dumps({"prompt": "a red tarp", "image": " tarp.jpg ", "verifier": "v"}))
    assert (args["image"], args["prompt"]) == ("tarp.jpg", "")
    assert main.parse_args(json.dumps({"image": "tarp.jpg", "verifier": "v"}))["image"] == "tarp.jpg"
    for bad in [
        {"verifier": "v"},
        {"prompt": " ", "image": "", "verifier": "v"},
        {"prompt": "p"},
        {"prompt": "p", "verifier": "v", "image": 3},
        {"prompt": "p", "verifier": "v", "log_tiles": "yes"},
        {"prompt": "p", "verifier": "v", "tile_size": 0},
        {"prompt": "p", "verifier": "v", "stride": 1.5},
        {"prompt": "p", "verifier": "v", "backgrounds": []},
        {"prompt": "p", "verifier": "v", "backgrounds": "grass"},
        {"prompt": "p", "verifier": "v", "quantized": "yes"},
        {"prompt": "p", "verifier": "v", "ema": 1},
    ]:
        with pytest.raises(main.ConfigError):
            main.parse_args(json.dumps(bad))


def test_the_quantized_model_runs_on_the_cpu():
    encoder = NpuImageEncoder(QUANTIZED_MODEL, qnn_dir=None, num_threads=2)
    rng = np.random.default_rng(0)
    tiles = [rng.integers(0, 256, (encoder.size, encoder.size, 3), dtype=np.uint8) for _ in range(2)]
    v = encoder.encode_tiles(tiles)
    assert v.shape == (2, 512)
    assert np.allclose(np.linalg.norm(v, axis=1), 1, atol=1e-5)


def test_log_tiles_writes_each_input_and_each_tile_sent(tmp_path):
    encoder = BrightnessEncoder()
    cliped = Cliped(
        encoder,
        Selector(encoder.t, encoder.b, margin=0.25, max_send=1),
        SendQueue(max_size=8, resend_after_s=5.0),
        prompt="a white square",
        verifier=VERIFIER,
        tile_size=200,
        stride=150,
        tile_log=TileLog(tmp_path / "cliped"),
    )
    tile, _ = cliped.on_input(make_input(1, make_frame(bright=(150, 0, 350, 200), id=40)), now=0)
    boxes = tile_boxes(400, 300, 200, 150)
    assert tile.index == boxes.index((150, 0, 350, 200))
    # An input that sends nothing is still written
    cliped.on_input(make_input(2, make_frame(id=41)), now=1)
    assert sorted(p.name for p in (tmp_path / "cliped" / "in").iterdir()) == ["40.jpeg", "41.jpeg"]
    assert [p.name for p in (tmp_path / "cliped" / "out").iterdir()] == [f"40_{tile.index}.jpeg"]
    out = cv2.imread(str(tmp_path / "cliped" / "out" / f"40_{tile.index}.jpeg"))
    assert out.shape == (200, 200, 3) and out.mean() > 250
    assert cv2.imread(str(tmp_path / "cliped" / "in" / "40.jpeg")).shape == (300, 400, 3)


def test_an_image_prompt_is_embedded_by_the_tile_encoder():
    encoder = BrightnessEncoder()
    white = np.full((50, 80, 3), 255, dtype=np.uint8)
    assert image_vector(encoder, white) == pytest.approx([1.0, 0.0])


def test_each_tile_queued_is_logged_with_its_box(cliped, tmp_path, monkeypatch):
    import csv

    from cliped.sent_log import SentLog

    monkeypatch.setenv("STEELEAGLE_FILES", str(tmp_path))
    cliped.sent_log = SentLog("t/sent.csv")
    frame = make_frame(bright=(150, 0, 350, 200), id=40)
    cliped.on_input(make_input(1, frame), now=0)
    cliped.on_input(make_input(2, make_frame(id=41)), now=1)
    cliped.sent_log.close()
    rows = list(csv.DictReader(open(tmp_path / "t" / "sent.csv")))
    assert len(rows) == 1
    row = rows[0]
    assert int(row["frame_id"]) == 40
    parsed = frame_pb2.RawFrame.FromString(frame)
    assert int(row["timestamp_ns"]) == parsed.timestamp.ToNanoseconds()
    assert [int(row[k]) for k in ("left", "top", "right", "bottom")] == [150, 0, 350, 200]
    assert (int(row["frame_width"]), int(row["frame_height"])) == (400, 300)
    assert row["class"] == "a white square"


def test_log_sent_defaults_to_off():
    args = main.parse_args(json.dumps({"prompt": "person", "verifier": "v"}))
    assert args["log_sent"] == ""


def test_each_frame_scored_is_logged_whether_or_not_a_tile_is_picked(cliped, tmp_path, monkeypatch):
    import csv

    from cliped.sent_log import ScoredLog

    monkeypatch.setenv("STEELEAGLE_FILES", str(tmp_path))
    cliped.scored_log = ScoredLog("t/scored.csv")
    frame = make_frame(bright=(150, 0, 350, 200), id=40)
    cliped.on_input(make_input(1, frame), now=0)
    cliped.on_input(make_input(2, make_frame(id=41)), now=1)
    cliped.scored_log.close()
    rows = list(csv.DictReader(open(tmp_path / "t" / "scored.csv")))
    assert [int(r["frame_id"]) for r in rows] == [40, 41]
    assert int(rows[0]["timestamp_ns"]) == frame_pb2.RawFrame.FromString(frame).timestamp.ToNanoseconds()
    assert [int(r["picked"]) for r in rows] == [1, 0]
    assert all(int(r["tiles"]) > 0 for r in rows)
