"""Tests for boundaries.py (SCOPE.md §6/§7): multi-snapshot timeline
merging, AssetSpan computation, and the (trivial) loop-boundary selection
pass-through."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grave_robber.boundaries import (  # noqa: E402
    compute_asset_spans,
    merge_timeline_snapshots,
    select_loop_boundary,
)
from grave_robber.models import AssetBoundary, AssetSpan, RawMarker, TimingSegment  # noqa: E402


def _seg(index, duration_ticks=90_000, asset_boundary=False, uri=None):
    return TimingSegment(index=index, duration_ticks=duration_ticks, asset_boundary=asset_boundary, source_uri=uri)


# ── merge_timeline_snapshots ──────────────────────────────────────────────


def test_merge_single_snapshot_is_unchanged():
    snapshot = ([_seg(0, uri="a"), _seg(1, uri="b")], [], [])

    segments, markers, boundaries = merge_timeline_snapshots([snapshot])

    assert [s.index for s in segments] == [0, 1]
    assert [s.source_uri for s in segments] == ["a", "b"]


def test_merge_deduplicates_overlapping_polls_by_source_uri():
    """A live poll's window overlaps almost entirely with the previous
    poll's -- only genuinely new segments should be appended."""
    snapshot1 = ([_seg(0, uri="a"), _seg(1, uri="b")], [], [])
    snapshot2 = ([_seg(0, uri="a"), _seg(1, uri="b"), _seg(2, uri="c")], [], [])

    segments, _, _ = merge_timeline_snapshots([snapshot1, snapshot2])

    assert [s.source_uri for s in segments] == ["a", "b", "c"]
    assert [s.index for s in segments] == [0, 1, 2]


def test_merge_keeps_segments_with_no_uri_as_always_new():
    snapshot1 = ([_seg(0, uri=None)], [], [])
    snapshot2 = ([_seg(0, uri=None)], [], [])

    segments, _, _ = merge_timeline_snapshots([snapshot1, snapshot2])

    assert len(segments) == 2


def test_merge_remaps_marker_positions_onto_the_merged_timeline():
    snapshot1 = (
        [_seg(0, duration_ticks=100, uri="a"), _seg(1, duration_ticks=100, uri="b")],
        [RawMarker(source="daterange", pts_time_ticks=150, splice_command_b64="X")],
        [],
    )
    snapshot2 = (
        [_seg(0, duration_ticks=100, uri="a"), _seg(1, duration_ticks=100, uri="b"), _seg(2, duration_ticks=100, uri="c")],
        [RawMarker(source="daterange", pts_time_ticks=250, splice_command_b64="Y")],
        [],
    )

    _, markers, _ = merge_timeline_snapshots([snapshot1, snapshot2])

    # First marker sits in segment 1 (old start 100, old tick 150 -> +50
    # into it); merged segment 1 starts at 100 too (nothing changed for
    # the first two segments) -> unaffected: 150.
    # Second marker sits in segment 2 (old start 200, +50) of snapshot2's
    # OWN local numbering; merged segment 2 also starts at 200 -> 250.
    assert sorted(m.pts_time_ticks for m in markers) == [150, 250]


def test_merge_drops_duplicate_marker_observations_across_snapshots():
    """The exact same real SCTE-35 event, seen again in a later poll at the
    same merged position, must not be duplicated."""
    snapshot1 = (
        [_seg(0, duration_ticks=100, uri="a")],
        [RawMarker(source="daterange", pts_time_ticks=50, splice_command_b64="X")],
        [],
    )
    snapshot2 = (
        [_seg(0, duration_ticks=100, uri="a"), _seg(1, duration_ticks=100, uri="b")],
        [RawMarker(source="daterange", pts_time_ticks=50, splice_command_b64="X")],
        [],
    )

    _, markers, _ = merge_timeline_snapshots([snapshot1, snapshot2])

    assert len(markers) == 1


def test_merge_remaps_boundaries_onto_merged_indices():
    snapshot1 = (
        [_seg(0, uri="a"), _seg(1, uri="b", asset_boundary=True)],
        [],
        [AssetBoundary(segment_index=1, gap_ticks=500)],
    )
    snapshot2 = (
        [_seg(0, uri="a"), _seg(1, uri="b", asset_boundary=True), _seg(2, uri="c")],
        [],
        [AssetBoundary(segment_index=1, gap_ticks=500)],
    )

    _, _, boundaries = merge_timeline_snapshots([snapshot1, snapshot2])

    assert boundaries == [AssetBoundary(segment_index=1, gap_ticks=500)]


# ── compute_asset_spans ────────────────────────────────────────────────────


def test_asset_spans_single_span_when_no_boundaries():
    segments = [_seg(0, duration_ticks=100), _seg(1, duration_ticks=200)]

    spans = compute_asset_spans(segments)

    assert spans == [AssetSpan(first_segment_index=0, segment_count=2, start_ticks=0, duration_ticks=300)]


def test_asset_spans_splits_at_internal_boundaries():
    segments = [
        _seg(0, duration_ticks=100),
        _seg(1, duration_ticks=100, asset_boundary=True),
        _seg(2, duration_ticks=50),
    ]

    spans = compute_asset_spans(segments)

    assert spans == [
        AssetSpan(first_segment_index=0, segment_count=1, start_ticks=0, duration_ticks=100),
        AssetSpan(first_segment_index=1, segment_count=2, start_ticks=100, duration_ticks=150),
    ]


def test_asset_spans_empty_for_no_segments():
    assert compute_asset_spans([]) == []


# ── select_loop_boundary ───────────────────────────────────────────────────


def test_select_loop_boundary_is_identity():
    segments = [_seg(0), _seg(1)]

    assert select_loop_boundary(segments) == segments
