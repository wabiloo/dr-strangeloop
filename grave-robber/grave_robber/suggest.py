"""Smart range suggestions for the import wizard (SCOPE.md §8 step 3, extended).

Three sensible windows, each with the variant to import and how much of it is
backed by real media bytes:

- `complete` -- longest window in which EVERY segment has media in the archive
  (a loop that plays with no 404s);
- `longest`  -- longest continuous manifest coverage of a single variant
  (missing media allowed -- pair with `allow_missing_segments`);
- `widest`   -- window fully covered by the most variants (a full ABR ladder),
  longest such window.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from .availability import (
    Range,
    SegmentAvailability,
    covered_ranges,
    covers_fully,
    media_ranges,
    window_stats,
)

MIN_WIDEST_WINDOW = _dt.timedelta(seconds=10)


@dataclass(frozen=True)
class VariantSegments:
    manifest_url: str
    segments: list[SegmentAvailability]


def _duration(r: Range) -> _dt.timedelta:
    return r[1] - r[0]


def _describe(variants: list[VariantSegments], window: Range, preferred: str | None) -> dict:
    start, end = window
    survivors = [v for v in variants if covers_fully(covered_ranges(v.segments), start, end)]
    stats = {v.manifest_url: window_stats(v.segments, start, end) for v in survivors}

    def media_ratio(v: VariantSegments) -> float:
        total, with_media = stats[v.manifest_url]
        return with_media / total if total else 0.0

    best = next((v for v in survivors if v.manifest_url == preferred), None) or max(
        survivors, key=media_ratio
    )
    total, with_media = stats[best.manifest_url]
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "duration_seconds": _duration(window).total_seconds(),
        "manifest_url": best.manifest_url,
        "segments_total": total,
        "segments_with_media": with_media,
        "survivor_count": len(survivors),
        "variant_count": len(variants),
    }


def _longest(per_variant: list[tuple[VariantSegments, list[Range]]]) -> tuple[Range, str] | None:
    candidates = [(r, v.manifest_url) for v, ranges in per_variant for r in ranges]
    return max(candidates, key=lambda c: _duration(c[0])) if candidates else None


def _widest(variants: list[VariantSegments]) -> tuple[Range, str | None] | None:
    ranges = [covered_ranges(v.segments) for v in variants]
    points = sorted({p for rs in ranges for r in rs for p in r})
    for k in range(len(variants), 0, -1):
        windows: list[Range] = []
        for a, b in zip(points, points[1:]):
            if sum(covers_fully(rs, a, b) for rs in ranges) >= k:
                if windows and windows[-1][1] == a:
                    windows[-1] = (windows[-1][0], b)
                else:
                    windows.append((a, b))
        windows = [w for w in windows if _duration(w) >= MIN_WIDEST_WINDOW]
        if windows:
            return max(windows, key=_duration), None
    return None


def suggest_ranges(variants: list[VariantSegments]) -> list[dict]:
    """Up to 3 suggestions (fewer when the archive has nothing to offer for
    one), in the order complete / longest / widest."""
    variants = [v for v in variants if v.segments]
    if not variants:
        return []
    out: list[dict] = []

    complete = _longest([(v, media_ranges(v.segments)) for v in variants])
    if complete:
        out.append(
            {
                "id": "complete",
                "title": "Fully recoverable",
                "description": "Longest window where every segment's media is in the archive.",
                **_describe(variants, complete[0], complete[1]),
            }
        )
    longest = _longest([(v, covered_ranges(v.segments)) for v in variants])
    if longest:
        out.append(
            {
                "id": "longest",
                "title": "Longest continuous",
                "description": "Longest uninterrupted manifest coverage; some media may be missing.",
                **_describe(variants, longest[0], longest[1]),
            }
        )
    widest = _widest(variants)
    if widest:
        out.append(
            {
                "id": "widest",
                "title": "Widest ladder",
                "description": "Longest window covered by the most variants.",
                **_describe(variants, widest[0], widest[1]),
            }
        )
    return out
