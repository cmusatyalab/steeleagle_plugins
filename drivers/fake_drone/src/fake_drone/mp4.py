"""Reads the tracks of an MP4 file: what each is, its timing, and, for a timed
metadata track, its samples.

GStreamer's qtdemux doesn't hand out the samples of a timed metadata track
(an `mett` sample entry), such as the frame telemetry Parrot drones record, so
they are read here from the file's sample tables. Only the boxes needed for
that, and for the video's dimensions and framerate, are parsed.
"""
import struct
from collections import Counter
from dataclasses import dataclass, field

# Boxes whose payload is only other boxes, on the way to the ones read here.
_CONTAINERS = {b'moov', b'trak', b'mdia', b'minf', b'stbl', b'edts', b'tref'}


@dataclass
class Track:
    track_id: int = 0
    # hdlr handler type, e.g. 'vide' or 'meta'
    handler: str = ''
    # Fourcc of the first sample entry, e.g. 'avc1' or 'mett'
    codec: str = ''
    # mime_format of a TextMetaDataSampleEntry ('mett'), e.g.
    # 'application/octet-stream;type=com.parrot.videometadata3'
    mime: str = ''
    # Visual sample entries' dimensions
    width: int = 0
    height: int = 0
    # Units per second of the track's times
    timescale: int = 1
    # (sample count, sample duration) runs, from stts
    durations: list = field(default_factory=list)
    sizes: list = field(default_factory=list)
    chunk_offsets: list = field(default_factory=list)
    # (first chunk, samples per chunk) runs, from stsc; chunks count from 1
    chunk_runs: list = field(default_factory=list)
    # (segment duration in movie units, media time in track units) edits
    edits: list = field(default_factory=list)
    # Ids of the tracks this one describes (tref cdsc)
    describes: list = field(default_factory=list)

    @property
    def sample_duration(self):
        """The most common sample duration, in track units."""
        counts = Counter()
        for count, duration in self.durations:
            counts[duration] += count
        return counts.most_common(1)[0][0] if counts else 0


@dataclass
class Movie:
    timescale: int = 1
    tracks: list = field(default_factory=list)


def read_movie(path):
    """The Movie in the MP4 file at `path`. Raises ValueError if it has none."""
    with open(path, 'rb') as f:
        for kind, start, size in _boxes(f, 0, _file_size(f)):
            if kind == b'moov':
                movie = Movie()
                _read_moov(f, start, size, movie)
                return movie
    raise ValueError(f'{path} has no moov box')


def sample_times(movie, track):
    """The presentation time, in seconds, of each of `track`'s samples, after
    its edit list: an empty edit delays the track, and the first edit's media
    time is where its presentation starts. Later edits are not followed."""
    delay, start = 0.0, 0
    for segment_duration, media_time in track.edits:
        if media_time == -1:
            delay += segment_duration / movie.timescale
            continue
        start = media_time
        break
    times = []
    t = 0
    for count, duration in track.durations:
        for _ in range(count):
            times.append(delay + (t - start) / track.timescale)
            t += duration
    return times


def read_samples(path, track):
    """The bytes of each of `track`'s samples, in order."""
    samples = []
    sample = 0
    with open(path, 'rb') as f:
        for chunk, offset in enumerate(track.chunk_offsets, start=1):
            per_chunk = _samples_per_chunk(track.chunk_runs, chunk)
            f.seek(offset)
            for _ in range(per_chunk):
                if sample >= len(track.sizes):
                    return samples
                samples.append(f.read(track.sizes[sample]))
                sample += 1
    return samples


def _samples_per_chunk(runs, chunk):
    per_chunk = 0
    for first_chunk, samples in runs:
        if first_chunk > chunk:
            break
        per_chunk = samples
    return per_chunk


def _file_size(f):
    f.seek(0, 2)
    return f.tell()


def _boxes(f, start, end):
    """(type, payload start, payload size) of each box from `start` to `end`."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        size, kind = struct.unpack('>I4s', f.read(8))
        header = 8
        if size == 1:
            size, = struct.unpack('>Q', f.read(8))
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            return
        yield kind, pos + header, size - header
        pos += size


def _read_moov(f, start, size, movie, track=None):
    for kind, child, child_size in _boxes(f, start, start + size):
        if kind == b'trak':
            track = Track()
            movie.tracks.append(track)
            _read_moov(f, child, child_size, movie, track)
        elif kind in _CONTAINERS:
            _read_moov(f, child, child_size, movie, track)
        elif kind == b'mvhd':
            _read_mvhd(_read(f, child, child_size), movie)
        elif track is not None:
            _read_track_box(kind, _read(f, child, child_size), track)


def _read(f, start, size):
    f.seek(start)
    return f.read(size)


def _read_mvhd(data, movie):
    version = data[0]
    movie.timescale, = struct.unpack_from('>I', data, 20 if version == 1 else 12)


def _read_track_box(kind, data, track):
    if kind == b'tkhd':
        version = data[0]
        track.track_id, = struct.unpack_from('>I', data, 20 if version == 1 else 12)
    elif kind == b'cdsc':
        track.describes = list(struct.unpack(f'>{len(data) // 4}I', data[:len(data) // 4 * 4]))
    elif kind == b'mdhd':
        version = data[0]
        track.timescale, = struct.unpack_from('>I', data, 20 if version == 1 else 12)
    elif kind == b'hdlr':
        track.handler = data[8:12].decode('latin-1')
    elif kind == b'stsd':
        _read_stsd(data, track)
    elif kind == b'stts':
        count, = struct.unpack_from('>I', data, 4)
        track.durations = [struct.unpack_from('>II', data, 8 + 8 * i) for i in range(count)]
    elif kind == b'stsz':
        sample_size, count = struct.unpack_from('>II', data, 4)
        if sample_size:
            track.sizes = [sample_size] * count
        else:
            track.sizes = list(struct.unpack_from(f'>{count}I', data, 12))
    elif kind == b'stsc':
        count, = struct.unpack_from('>I', data, 4)
        track.chunk_runs = [struct.unpack_from('>II', data, 8 + 12 * i) for i in range(count)]
    elif kind == b'stco':
        count, = struct.unpack_from('>I', data, 4)
        track.chunk_offsets = list(struct.unpack_from(f'>{count}I', data, 8))
    elif kind == b'co64':
        count, = struct.unpack_from('>I', data, 4)
        track.chunk_offsets = list(struct.unpack_from(f'>{count}Q', data, 8))
    elif kind == b'elst':
        version = data[0]
        count, = struct.unpack_from('>I', data, 4)
        entry = '>Qq4x' if version == 1 else '>Ii4x'
        entry_size = struct.calcsize(entry)
        track.edits = [struct.unpack_from(entry, data, 8 + entry_size * i) for i in range(count)]


def _read_stsd(data, track):
    count, = struct.unpack_from('>I', data, 4)
    if count == 0 or len(data) < 24:
        return
    # The first sample entry, after the full box header and entry count: its
    # size and fourcc, 6 reserved bytes and a data reference index, then the
    # entry's own fields.
    size, codec = struct.unpack_from('>I4s', data, 8)
    entry = data[24:8 + size]
    track.codec = codec.decode('latin-1')
    if track.codec == 'mett':
        # content_encoding and mime_format, both null-terminated
        fields = entry.split(b'\0')
        if len(fields) >= 2:
            track.mime = fields[1].decode('utf-8', 'replace')
    elif track.handler == 'vide' and len(entry) >= 28:
        # VisualSampleEntry: 16 bytes of reserved/pre-defined, then the size
        track.width, track.height = struct.unpack_from('>HH', entry, 16)
