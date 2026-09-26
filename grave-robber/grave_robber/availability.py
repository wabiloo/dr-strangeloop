"""Per-variant segment availability (SCOPE.md §8, extended): which unique
segments of an HLS variant are positioned on the wall-clock timeline, and
whether their media bytes actually exist in the archive. Manifest coverage
alone (coverage.py) says a segment was *listed*; this says whether it can
be *served* -- the two differ whenever the player didn't fetch a segment."""

from __future__ import annotations

import dataclasses
import datetime as _dt
from dataclasses import dataclass

from trace_shrink import Format, Trace

from .audio import align_by_start
from .boundaries import merge_timeline_snapshots
from .media_extract import has_media
from .pipeline import extract_snapshots

TOLERANCE = _dt.timedelta(milliseconds=50)

Range = tuple[_dt.datetime, _dt.datetime]


@dataclass(frozen=True)
class SegmentAvailability:
    start: _dt.datetime
    end: _dt.datetime
    has_media: bool


def build_variant_segments(trace: Trace, manifest_url: str) -> list[SegmentAvailability]:
    """Unique wall-clock-positioned segments of one HLS variant, in time
    order. Segments with no PROGRAM-DATE-TIME reference are omitted."""
    snapshots = extract_snapshots(trace, manifest_url, Format.HLS)
    segments, _markers, _boundaries = merge_timeline_snapshots(snapshots)
    out = [
        SegmentAvailability(
            start=s.start_time,
            end=s.start_time + _dt.timedelta(seconds=s.duration_ticks / 90_000),
            has_media=has_media(trace, s.source_uri, s.init_uri),
        )
        for s in segments
        if s.start_time is not None
    ]
    return sorted(out, key=lambda s: s.start)


def combine_audio(
    video: list[SegmentAvailability], audio: list[SegmentAvailability]
) -> list[SegmentAvailability]:
    """Fold a separate audio playlist's availability into the video's: a
    segment is only "recoverable" when its audio (aligned by wall-clock, as at
    ingest) is in the archive too. No audio segments at all (e.g. no
    PROGRAM-DATE-TIME) leaves the video availability unchanged."""
    if not audio:
        return video
    aligned = align_by_start(video, audio, lambda s: s.start, lambda s: (s.end - s.start).total_seconds())
    return [
        dataclasses.replace(v, has_media=v.has_media and a is not None and a.has_media)
        for v, a in zip(video, aligned)
    ]


def merge_ranges(ranges: list[Range], tolerance: _dt.timedelta = TOLERANCE) -> list[Range]:
    merged: list[Range] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + tolerance:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def covered_ranges(segments: list[SegmentAvailability]) -> list[Range]:
    return merge_ranges([(s.start, s.end) for s in segments])


def media_ranges(segments: list[SegmentAvailability]) -> list[Range]:
    return merge_ranges([(s.start, s.end) for s in segments if s.has_media])


def covers_fully(ranges: list[Range], start: _dt.datetime, end: _dt.datetime) -> bool:
    return any(r_start <= start + TOLERANCE and r_end >= end - TOLERANCE for r_start, r_end in ranges)


def window_stats(segments: list[SegmentAvailability], start: _dt.datetime, end: _dt.datetime) -> tuple[int, int]:
    """(segments fully inside the window, of which with media)."""
    inside = [s for s in segments if s.start >= start - TOLERANCE and s.end <= end + TOLERANCE]
    return len(inside), sum(1 for s in inside if s.has_media)
