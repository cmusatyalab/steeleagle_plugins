import math

import pytest
from conftest import PRODUCER, SESSION, make_frame

from storage.store import ENCODED_FRAME_TYPE, FrameKey, FrameStore, frame_index


def add(store, seq, **pose):
    frame = make_frame(data=f"frame {seq}".encode(), **pose)
    store.add(
        FrameKey(PRODUCER, SESSION, seq),
        ENCODED_FRAME_TYPE,
        frame.SerializeToString(),
        frame_index(frame),
    )


def test_unset_index_fields_are_nan():
    index = frame_index(make_frame(lat=40.5, angle=90.0))
    assert index["latitude"] == 40.5
    assert index["relative_heading"] == 90.0
    unset = [name for name, value in index.items() if math.isnan(value)]
    assert sorted(unset) == sorted(
        ["longitude", "altitude", "heading", "x", "y", "z"]
    )


def test_a_frame_without_position_info_is_all_nan():
    assert all(math.isnan(v) for v in frame_index(make_frame()).values())


def test_a_stored_frame_comes_back_under_its_key_and_uint64_session(store):
    add(store, 7, lat=1.0)
    key = FrameKey(PRODUCER, SESSION, 7)
    assert key in store
    stored = store.get(key)
    assert stored.key == key
    assert stored.payload_type == ENCODED_FRAME_TYPE
    assert store.by_seq(7).key == key
    assert store.by_seq(8) is None


def test_best_match_has_the_lowest_mean_squared_error(store):
    add(store, 1, x=0.0, y=0.0)
    add(store, 2, x=10.0, y=10.0)
    add(store, 3, x=4.0, y=6.0)
    assert store.best_match({"x": 5.0, "y": 5.0}).key.seq == 3
    assert store.best_match({"x": 9.0}).key.seq == 2


def test_best_match_skips_frames_without_a_queried_field(store):
    add(store, 1, x=5.0)  # no y
    add(store, 2, x=100.0, y=100.0)
    assert store.best_match({"x": 5.0, "y": 5.0}).key.seq == 2
    assert store.best_match({"latitude": 1.0}) is None


def test_headings_wrap_around(store):
    add(store, 1, heading=350.0)
    add(store, 2, heading=40.0)
    assert store.best_match({"heading": 5.0}).key.seq == 1


def test_a_query_must_set_a_known_field(store):
    with pytest.raises(ValueError):
        store.best_match({})
    with pytest.raises(ValueError):
        store.best_match({"speed": 1.0})


def test_frames_persist_in_a_file(tmp_path):
    path = str(tmp_path / "frames.sqlite3")
    with FrameStore(path) as store:
        add(store, 3, z=2.0)
    with FrameStore(path) as store:
        assert store.best_match({"z": 2.0}).key.seq == 3
