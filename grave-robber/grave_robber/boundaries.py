"""Format-agnostic asset-boundary/gap-overlap modeling (SCOPE.md §6) and
loop-boundary selection (§7), operating on the normalized TimingSegment/
RawMarker/AssetBoundary lists extract_hls.py/extract_dash.py produce per
manifest snapshot.

A captured session is typically many manifest snapshots (repeated polls of
a live playlist/MPD over the capture window, via `trace_shrink`'s
`ManifestStream`), each showing an overlapping window of segments as the
live edge advances. `merge_timeline_snapshots` reconstructs one coherent,
deduplicated timeline from that -- SCOPE.md §7's "full captured span"
loop-boundary rule falls straight out of this: the first snapshot's
earliest still-new segment starts the merged timeline, the last snapshot's
latest segment ends it, with no separate trimming step needed.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt

from .models import AssetBoundary, AssetSpan, RawMarker, TimingSegment


def merge_timeline_snapshots(
    snapshots: list[tuple[list[TimingSegment], list[RawMarker], list[AssetBoundary]]],
) -> tuple[list[TimingSegment], list[RawMarker], list[AssetBoundary]]:
    """Combine multiple manifest snapshots (chronological order) into one
    overall timeline (SCOPE.md §7's "full captured span" rule).

    Segments are deduplicated by `source_uri` (a live poll's window
    overlaps almost entirely with the previous poll's -- only a handful of
    segments are genuinely new each time), keeping the FIRST time each URI
    is seen, in chronological order. A segment with no `source_uri` (a
    format extractor's own fallback -- see TimingSegment's docstring) can't
    be deduplicated this way and is always kept as its own new segment.

    Markers/boundaries are re-expressed against the merged timeline's new
    segment indices; an entry positioned against a segment that ultimately
    got deduplicated away is dropped as a duplicate observation of the same
    real event (identical `pts_time_ticks` on the surviving occurrence,
    since both were derived from the same, byte-identical manifest
    fragment for that segment).
    """
    seen_uris: set[str] = set()
    per_snapshot_index_map: list[dict[int, int]] = []
    merged_segments: list[TimingSegment] = []

    for segments, _markers, _boundaries in snapshots:
        index_map: dict[int, int] = {}
        for segment in segments:
            if segment.source_uri is not None and segment.source_uri in seen_uris:
                continue
            if segment.source_uri is not None:
                seen_uris.add(segment.source_uri)
            new_index = len(merged_segments)
            index_map[segment.index] = new_index
            merged_segments.append(dataclasses.replace(segment, index=new_index))
        per_snapshot_index_map.append(index_map)

    boundary_ticks = _cumulative_start_ticks(merged_segments)

    merged_markers: list[RawMarker] = []
    seen_marker_keys: set[tuple[str, int]] = set()
    for (segments, markers, _boundaries), index_map in zip(snapshots, per_snapshot_index_map):
        old_starts = _cumulative_start_ticks(segments)
        for marker in markers:
            old_index = _segment_index_for_tick(marker.pts_time_ticks, old_starts)
            if old_index is None or old_index not in index_map:
                continue  # positioned against a segment this snapshot no longer contributes
            new_start = boundary_ticks[index_map[old_index]]
            old_start = old_starts[old_index]
            new_pts_time_ticks = marker.pts_time_ticks - old_start + new_start
            key = (marker.source, new_pts_time_ticks)
            if key in seen_marker_keys:
                continue
            seen_marker_keys.add(key)
            merged_markers.append(dataclasses.replace(marker, pts_time_ticks=new_pts_time_ticks))

    merged_boundaries: list[AssetBoundary] = []
    seen_boundary_indices: set[int] = set()
    for (_segments, _markers, boundaries), index_map in zip(snapshots, per_snapshot_index_map):
        for boundary in boundaries:
            if boundary.segment_index not in index_map:
                continue
            new_index = index_map[boundary.segment_index]
            if new_index in seen_boundary_indices:
                continue
            seen_boundary_indices.add(new_index)
            merged_boundaries.append(dataclasses.replace(boundary, segment_index=new_index))
    merged_boundaries.sort(key=lambda b: b.segment_index)

    return merged_segments, merged_markers, merged_boundaries


def _cumulative_start_ticks(segments: list[TimingSegment]) -> list[int]:
    starts = []
    running = 0
    for segment in segments:
        starts.append(running)
        running += segment.duration_ticks
    return starts


def _segment_index_for_tick(tick: int, starts: list[int]) -> int | None:
    if not starts:
        return None
    index = 0
    for i, start in enumerate(starts):
        if start <= tick:
            index = i
        else:
            break
    return index


def compute_asset_spans(segments: list[TimingSegment]) -> list[AssetSpan]:
    """SCOPE.md §5's AssetSpan: contiguous runs of segments between asset
    boundaries."""
    if not segments:
        return []

    spans: list[AssetSpan] = []
    span_start_index = 0
    span_start_ticks = 0
    span_duration_ticks = 0

    for index, segment in enumerate(segments):
        if segment.asset_boundary and index != 0:
            spans.append(
                AssetSpan(
                    first_segment_index=span_start_index,
                    segment_count=index - span_start_index,
                    start_ticks=span_start_ticks,
                    duration_ticks=span_duration_ticks,
                )
            )
            span_start_index = index
            span_start_ticks += span_duration_ticks
            span_duration_ticks = 0
        span_duration_ticks += segment.duration_ticks

    spans.append(
        AssetSpan(
            first_segment_index=span_start_index,
            segment_count=len(segments) - span_start_index,
            start_ticks=span_start_ticks,
            duration_ticks=span_duration_ticks,
        )
    )
    return spans


def select_loop_boundary(
    segments: list[TimingSegment],
) -> list[TimingSegment]:
    """SCOPE.md §7: **Decision** -- use the archive's full captured span,
    without auto-detection or fingerprinting. The first manifest snapshot's
    earliest segment defines the start of the first AssetSpan; the last
    snapshot's latest segment defines the end of the last. Since
    `merge_timeline_snapshots` already assembles exactly that (chronological
    dedup, nothing trimmed), this is intentionally an identity pass-through
    -- kept as its own named step so the "no auto-detection" decision has
    one obvious place to live/be revisited, rather than being implicit in
    merge_timeline_snapshots's own docstring."""
    return segments


def trim_timeline(
    segments: list[TimingSegment],
    markers: list[RawMarker],
    boundaries: list[AssetBoundary],
    start: _dt.datetime,
    end: _dt.datetime,
    *,
    tolerance: _dt.timedelta = _dt.timedelta(milliseconds=50),
) -> tuple[list[TimingSegment], list[RawMarker], list[AssetBoundary]]:
    """Restrict a merged timeline to the segments lying fully inside the
    wall-clock window [start, end] (SCOPE.md §7, amended: the picked range
    trims the loop). Segments without a wall-clock `start_time` are dropped.
    Segment indices, marker ticks and boundary indices are re-expressed
    against the trimmed timeline; markers outside it are dropped."""
    kept = [
        s
        for s in segments
        if s.start_time is not None
        and s.start_time >= start - tolerance
        and s.start_time + _dt.timedelta(seconds=s.duration_ticks / 90_000) <= end + tolerance
    ]
    if not kept:
        return [], [], []

    starts = _cumulative_start_ticks(segments)
    removed_ticks = starts[kept[0].index]
    index_map = {s.index: new_index for new_index, s in enumerate(kept)}
    new_segments = [dataclasses.replace(s, index=index_map[s.index]) for s in kept]
    total_ticks = sum(s.duration_ticks for s in new_segments)

    new_markers = [
        dataclasses.replace(m, pts_time_ticks=m.pts_time_ticks - removed_ticks)
        for m in markers
        if 0 <= m.pts_time_ticks - removed_ticks < total_ticks
    ]
    new_boundaries = [
        dataclasses.replace(b, segment_index=index_map[b.segment_index], gap_ticks=0 if index_map[b.segment_index] == 0 else b.gap_ticks)
        for b in boundaries
        if b.segment_index in index_map
    ]
    return new_segments, new_markers, new_boundaries
