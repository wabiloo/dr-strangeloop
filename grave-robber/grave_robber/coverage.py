"""Multi-variant HLS coverage + filtering (SCOPE.md §8).

An HLS ABR ladder captured from a real OTT player reflects that player's
*adaptation* decisions, not a uniform recording window -- different
variants may have been requested for different, non-contiguous stretches
of the session. This module builds a per-variant wall-clock coverage map
(step 1), merges/exposes it for a human-selected range (step 2, normally
rendered by `igor` -- see `grave-robber/SCOPE.md` §10), filters the
ladder down to full-coverage survivors for a chosen range (steps 3-4), and
picks an arbitrary (but stable) reference among the survivors (step 5).

DASH multi-period sources get the equivalent treatment via
`build_dash_variant_coverage` for completeness (SCOPE.md §8's closing
note), though the "player adaptation switching mid-capture" motivation is
HLS-specific.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

import m3u8

from .extract_dash import MPDInspector, MPDParser
from .extract_hls import assign_program_date_times

TIMESCALE = 90_000


@dataclass(frozen=True)
class VariantCoverage:
    """A single rendition/variant's captured wall-clock coverage --
    `covered_ranges` is sorted, non-overlapping [start, end) datetime
    pairs. Gaps between manifest polls where this variant wasn't the
    active one show up directly as holes between ranges (SCOPE.md §8
    step 1)."""

    name: str
    covered_ranges: list[tuple[_dt.datetime, _dt.datetime]]


def _merge_ranges(
    ranges: list[tuple[_dt.datetime, _dt.datetime]],
) -> list[tuple[_dt.datetime, _dt.datetime]]:
    if not ranges:
        return []
    ordered = sorted(ranges, key=lambda r: r[0])
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def build_hls_variant_coverage(
    name: str, manifest_snapshots: list[tuple[str, str]]
) -> VariantCoverage:
    """`manifest_snapshots`: this ONE variant's own manifest text captured
    over time, as (manifest_text, manifest_url) pairs in any order (sorted
    internally by the ranges' own start time via `_merge_ranges`) -- e.g.
    every entry `trace.get_entries_for_url` returns for this variant's own
    playlist URL, each decoded to text.

    Each segment with a known PROGRAM-DATE-TIME (per
    `extract_hls.assign_program_date_times`'s same forward-carry/
    per-discontinuity-run rules) contributes its own covered wall-clock
    interval; segments with no PDT reference at all contribute nothing
    (there's no wall-clock position to compare against other variants --
    a known limitation for a source with no PDT signaling whatsoever, see
    SCOPE.md §8's "not yet spiked against a real archive" open item)."""
    ranges: list[tuple[_dt.datetime, _dt.datetime]] = []
    for manifest_text, manifest_url in manifest_snapshots:
        playlist = m3u8.loads(manifest_text, uri=manifest_url)
        pdts = assign_program_date_times(playlist.segments)
        for segment, pdt in zip(playlist.segments, pdts):
            if pdt is None:
                continue
            end = pdt + _dt.timedelta(seconds=segment.duration or 0.0)
            ranges.append((pdt, end))
    return VariantCoverage(name=name, covered_ranges=_merge_ranges(ranges))


def build_dash_variant_coverage(
    name: str, manifest_snapshots: list[tuple[str, str]]
) -> VariantCoverage:
    """DASH equivalent (SCOPE.md §8's closing note): each Period's own
    `start_time` (already absolute wall-clock for a dynamic MPD --
    `PeriodInspector.start_time`) plus its `duration` gives the covered
    interval directly, no PDT-style cross-referencing needed."""
    ranges: list[tuple[_dt.datetime, _dt.datetime]] = []
    for manifest_text, manifest_url in manifest_snapshots:
        mpd = MPDParser.from_string(manifest_text)
        inspector = MPDInspector(mpd)
        inspector.base_uri = manifest_url.rsplit("/", 1)[0] + "/" if manifest_url else ""
        for period in inspector.periods:
            start = period.start_time
            duration = period.duration
            if not isinstance(start, _dt.datetime) or duration is None:
                continue  # static/VOD MPD (timedelta start) has no wall-clock to compare
            ranges.append((start, start + duration))
    return VariantCoverage(name=name, covered_ranges=_merge_ranges(ranges))


def _covers_fully(
    covered_ranges: list[tuple[_dt.datetime, _dt.datetime]],
    target_start: _dt.datetime,
    target_end: _dt.datetime,
) -> bool:
    """Whether `covered_ranges` (sorted, non-overlapping) is a superset of
    [target_start, target_end) with zero gaps across the entire range."""
    cursor = target_start
    for range_start, range_end in covered_ranges:
        if range_start > cursor:
            break  # a gap right where we need coverage
        if range_end > cursor:
            cursor = range_end
        if cursor >= target_end:
            return True
    return cursor >= target_end


def filter_variants_covering_range(
    variants: list[VariantCoverage],
    target_range: tuple[_dt.datetime, _dt.datetime],
) -> tuple[list[VariantCoverage], list[VariantCoverage]]:
    """SCOPE.md §8 steps 3-4: keep only variants whose coverage is a
    superset of the human-picked [start, end) range; drop everything else
    from the output ladder entirely (not trimmed/reconciled, excluded).
    Returns (survivors, dropped)."""
    target_start, target_end = target_range
    survivors: list[VariantCoverage] = []
    dropped: list[VariantCoverage] = []
    for variant in variants:
        if _covers_fully(variant.covered_ranges, target_start, target_end):
            survivors.append(variant)
        else:
            dropped.append(variant)
    return survivors, dropped


def pick_reference_variant(survivors: list[VariantCoverage]) -> VariantCoverage:
    """SCOPE.md §8 step 5: once every survivor shares the exact same
    wall-clock window, the tick-reference pick is low-stakes -- arbitrary
    (first survivor) is fine. Stable/deterministic given a stable input
    order (callers should pass survivors in a consistent order, e.g. ladder
    bitrate order, if "first" should mean something specific)."""
    if not survivors:
        raise ValueError("No surviving variants to pick a reference from")
    return survivors[0]
