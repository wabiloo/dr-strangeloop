"""Output writer: the segment-list manifest + markers.json-shaped list
loop-dee-loop's `bake.py` sparse input mode consumes (loop-dee-loop/
SCOPE.md §11.2) -- this tool's only real output shape, decided in
grave-robber/SCOPE.md §11 ("output feeds bake.py's new sparse segment-list
input mode, not `--markers-override`").
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import AssetBoundary, TimingSegment


def build_segment_list_manifest(
    segments: list[TimingSegment],
    boundaries: list[AssetBoundary],
    decoded_markers: list[dict],
    media_paths: dict[int, Path | None],
) -> dict:
    """Assemble the segment-list manifest dict (loop-dee-loop/SCOPE.md
    §11.2's shape, extended with the optional `gap_ticks` field --
    loop-dee-loop's own SCOPE.md §11.2 addendum). `media_paths` (from
    media_extract.extract_media_for_segments) may map an index to `None`
    -- carried straight through as `media_file: null`, exactly what the
    sparse bake mode expects for a segment with no recovered media
    (SCOPE.md §1's revised stance)."""
    gap_ticks_by_index = {b.segment_index: b.gap_ticks for b in boundaries if b.gap_ticks != 0}
    asset_boundary_indices = {b.segment_index for b in boundaries}

    segment_entries = []
    for segment in segments:
        is_boundary = segment.asset_boundary or segment.index in asset_boundary_indices
        media_path = media_paths.get(segment.index)
        entry: dict = {
            "index": segment.index,
            "duration_ticks": segment.duration_ticks,
            "asset_boundary": is_boundary,
            "media_file": str(media_path) if media_path is not None else None,
        }
        gap_ticks = gap_ticks_by_index.get(segment.index, 0)
        if gap_ticks:
            entry["gap_ticks"] = gap_ticks
        segment_entries.append(entry)

    # decode_marker's own "source" provenance field (daterange/cue-out/
    # eventstream-bin/eventstream-xml) isn't part of loop-dee-loop's
    # markers.json shape -- drop it here rather than leaking an
    # undocumented extra key into bake.py's input.
    markers_out = [{k: v for k, v in m.items() if k != "source"} for m in decoded_markers]

    return {"segments": segment_entries, "markers": markers_out}


def write_segment_list_manifest(manifest: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
