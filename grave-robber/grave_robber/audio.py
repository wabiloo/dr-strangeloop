"""Audio handling for archive imports (HLS).

Audio reaches the archive one of two ways:
- muxed into the video segments (nothing to do here -- `bake.py` detects it);
- as its own playlist (`#EXT-X-MEDIA:TYPE=AUDIO`) with its own segments. Those
  are aligned to the (already trimmed) video segments here, so the segment
  list can carry one audio entry per video segment.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
from typing import Callable, TypeVar

from .models import TimingSegment

T = TypeVar("T")


def align_by_start(
    video: list[T],
    audio: list[T],
    start_of: Callable[[T], _dt.datetime | None],
    duration_seconds_of: Callable[[T], float],
) -> list[T | None]:
    """For each video item, the audio item starting nearest to it (within half
    the video item's duration), or None. Falls back to index alignment when
    wall-clock isn't available on both sides and the counts match."""
    if not audio:
        return [None] * len(video)
    if all(start_of(v) is not None for v in video) and all(start_of(a) is not None for a in audio):
        out: list[T | None] = []
        for v in video:
            tolerance = _dt.timedelta(seconds=duration_seconds_of(v) / 2)
            best = min(audio, key=lambda a: abs(start_of(a) - start_of(v)))
            out.append(best if abs(start_of(best) - start_of(v)) <= tolerance else None)
        return out
    if len(video) == len(audio):
        return list(audio)
    raise ValueError(
        "Can't align the audio playlist to the video: no PROGRAM-DATE-TIME on one side "
        f"and different segment counts ({len(audio)} audio vs {len(video)} video)."
    )


def align_audio_segments(video: list[TimingSegment], audio: list[TimingSegment]) -> list[TimingSegment | None]:
    """Audio TimingSegments re-indexed to the video segment they belong to
    (None where the audio playlist has nothing for that video segment)."""
    aligned = align_by_start(
        video, audio, lambda s: s.start_time, lambda s: s.duration_ticks / 90_000
    )
    return [
        dataclasses.replace(a, index=v.index) if a is not None else None for v, a in zip(video, aligned)
    ]


def align_audio_segments_by_ticks(video: list[TimingSegment], audio: list[TimingSegment]) -> list[TimingSegment | None]:
    """Same as `align_audio_segments` for sources with no wall-clock (DASH): both
    sides are placed on their own cumulative tick timeline, and each video segment
    takes the audio segment starting nearest to it (within half the video
    segment's duration), or None."""

    def _starts(segments: list[TimingSegment]) -> list[int]:
        out, total = [], 0
        for s in segments:
            out.append(total)
            total += s.duration_ticks
        return out

    if not audio:
        return [None] * len(video)
    video_starts, audio_starts = _starts(video), _starts(audio)
    aligned: list[TimingSegment | None] = []
    for v, v_start in zip(video, video_starts):
        best = min(range(len(audio)), key=lambda i: abs(audio_starts[i] - v_start))
        close = abs(audio_starts[best] - v_start) <= v.duration_ticks / 2
        aligned.append(dataclasses.replace(audio[best], index=v.index) if close else None)
    return aligned
