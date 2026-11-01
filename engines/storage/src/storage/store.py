"""The frame store both storage plugins share: a SQLite table of encoded
frames, keyed by their Uriel input id and indexed by the vehicle's pose when
each was captured."""

import math
import sqlite3
from dataclasses import dataclass

from google.protobuf.message import DecodeError

from steeleagle_protocol.v1.messages.frame import frame_pb2

# The index fields, in table column order. Headings are angles, so their
# differences wrap around at 360 degrees.
INDEX_FIELDS = (
    "latitude",
    "longitude",
    "altitude",
    "heading",
    "x",
    "y",
    "z",
    "relative_heading",
)
ANGLE_FIELDS = frozenset({"heading", "relative_heading"})

ENCODED_FRAME_TYPE = "steeleagle_protocol/v1/messages/frame/EncodedFrame"

_UINT64 = 1 << 64
_INT64_MAX = (1 << 63) - 1


class FrameError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class FrameKey:
    """Identifies a stored frame: the Uriel InputId it arrived under."""

    producer: str
    session: int
    seq: int

    @classmethod
    def of(cls, id):
        """The key of a uriel.InputId."""
        return cls(id.producer, id.session, id.seq)


@dataclass(frozen=True, slots=True)
class StoredFrame:
    key: FrameKey
    payload_type: str
    data: bytes


def frame_index(frame):
    """The index fields of an EncodedFrame (or RawFrame), by name. A field the
    frame does not set is NaN."""
    index = dict.fromkeys(INDEX_FIELDS, math.nan)
    if not frame.HasField("position_info"):
        return index
    position = frame.position_info
    if position.HasField("global_position"):
        g = position.global_position
        for name, field in (
            ("latitude", "latitude"),
            ("longitude", "longitude"),
            ("altitude", "altitude"),
            ("heading", "heading"),
        ):
            if g.HasField(field):
                index[name] = getattr(g, field)
    if position.HasField("relative_position"):
        r = position.relative_position
        for name, field in (
            ("x", "x"),
            ("y", "y"),
            ("z", "z"),
            ("relative_heading", "angle"),
        ):
            if r.HasField(field):
                index[name] = getattr(r, field)
    return index


def _signed(session):
    """A uint64 session as SQLite's signed 64-bit integer, and back."""
    return session - _UINT64 if session > _INT64_MAX else session


def _unsigned(session):
    return session + _UINT64 if session < 0 else session


def _angle_error(a, b):
    """The squared difference of two angles in degrees, the short way round."""
    d = (a - b + 180.0) % 360.0 - 180.0
    return d * d


def _column(value):
    """SQLite stores NaN as NULL, so an unset field is NULL in the table."""
    return None if math.isnan(value) else value


class FrameStore:
    """Encoded frames in a SQLite database at `path` (":memory:" for one in
    memory). It must be used on the thread that opened it."""

    def __init__(self, path):
        self._db = sqlite3.connect(path)
        self._db.create_function("angle_error", 2, _angle_error, deterministic=True)
        if path != ":memory:":
            # Fewer fsyncs: a frame lost in a power cut is no worse than a
            # frame the camera dropped
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
        columns = ", ".join(f"{name} REAL" for name in INDEX_FIELDS)
        self._db.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS frames (
                producer TEXT NOT NULL,
                session INTEGER NOT NULL,
                seq INTEGER NOT NULL,
                {columns},
                payload_type TEXT NOT NULL,
                data BLOB NOT NULL,
                PRIMARY KEY (producer, session, seq)
            );
            CREATE INDEX IF NOT EXISTS frames_by_seq ON frames (seq);
            """
        )

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def add(self, key, payload_type, data, index):
        """Stores the frame `data` under `key`, with its `index` (as
        `frame_index` gives it), replacing any frame stored under the key."""
        names = ", ".join(INDEX_FIELDS)
        marks = ", ".join("?" for _ in INDEX_FIELDS)
        with self._db:
            self._db.execute(
                f"INSERT OR REPLACE INTO frames (producer, session, seq, {names}, payload_type, data) "
                f"VALUES (?, ?, ?, {marks}, ?, ?)",
                (
                    key.producer,
                    _signed(key.session),
                    key.seq,
                    *(_column(index[name]) for name in INDEX_FIELDS),
                    payload_type,
                    bytes(data),
                ),
            )

    def __contains__(self, key):
        row = self._db.execute(
            "SELECT 1 FROM frames WHERE producer = ? AND session = ? AND seq = ?",
            (key.producer, _signed(key.session), key.seq),
        ).fetchone()
        return row is not None

    def __len__(self):
        return self._db.execute("SELECT COUNT(*) FROM frames").fetchone()[0]

    def get(self, key):
        """The frame stored under `key`, or None."""
        row = self._db.execute(
            "SELECT producer, session, seq, payload_type, data FROM frames "
            "WHERE producer = ? AND session = ? AND seq = ?",
            (key.producer, _signed(key.session), key.seq),
        ).fetchone()
        return _stored(row)

    def by_seq(self, seq):
        """The frame stored with sequence number `seq`, or None. If several
        producers or sessions stored one, the most recently stored."""
        row = self._db.execute(
            "SELECT producer, session, seq, payload_type, data FROM frames "
            "WHERE seq = ? ORDER BY rowid DESC LIMIT 1",
            (seq,),
        ).fetchone()
        return _stored(row)

    def best_match(self, query):
        """The frame whose index fields have the lowest mean squared error to
        `query`, a dict of some index fields to their wanted values, or None
        if no stored frame has all of them. Ties go to the most recently
        stored frame."""
        if not query:
            raise ValueError("the query sets no index field")
        unknown = set(query) - set(INDEX_FIELDS)
        if unknown:
            raise ValueError(f"unknown index fields {sorted(unknown)}")
        terms, present, params = [], [], []
        for name, value in query.items():
            present.append(f"{name} IS NOT NULL")
            if name in ANGLE_FIELDS:
                terms.append(f"angle_error({name}, ?)")
                params.append(value)
            else:
                terms.append(f"(({name} - ?) * ({name} - ?))")
                params.extend((value, value))
        row = self._db.execute(
            "SELECT producer, session, seq, payload_type, data FROM frames "
            f"WHERE {' AND '.join(present)} "
            f"ORDER BY ({' + '.join(terms)}) / {len(terms)} ASC, rowid DESC LIMIT 1",
            params,
        ).fetchone()
        return _stored(row)


def _stored(row):
    if row is None:
        return None
    producer, session, seq, payload_type, data = row
    return StoredFrame(FrameKey(producer, _unsigned(session), seq), payload_type, data)



def read_frame(payload_type, data):
    """The EncodedFrame in an input's `data`."""
    if payload_type != ENCODED_FRAME_TYPE:
        raise FrameError(f"payload is a {payload_type!r}, not a {ENCODED_FRAME_TYPE}")
    try:
        return frame_pb2.EncodedFrame.FromString(bytes(data))
    except DecodeError as err:
        raise FrameError(f"not an EncodedFrame: {err}") from err
