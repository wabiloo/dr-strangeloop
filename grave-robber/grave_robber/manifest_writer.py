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
    declared_variant: dict | None = None,
    audio_segments: list | None = None,
    audio_media_paths: dict[int, Path | None] | None = None,
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

    if audio_segments is not None:
        # A separate audio playlist, aligned 1:1 to the video segments.
        for entry, audio in zip(segment_entries, audio_segments):
            path = (audio_media_paths or {}).get(entry["index"]) if audio is not None else None
            entry["audio_media_file"] = str(path) if path is not None else None
            entry["audio_duration_ticks"] = audio.duration_ticks if audio is not None else entry["duration_ticks"]

    manifest = {"segments": segment_entries, "markers": markers_out}
    if audio_segments is not None:
        manifest["audio"] = {"separate": True}
    if declared_variant:
        # What the archive's multivariant playlist declared for this variant
        # (exact codecs/bandwidth) -- bake.py prefers it over re-deriving.
        manifest["variant"] = {
            k: declared_variant[k] for k in ("codecs", "bandwidth", "resolution", "frame_rate") if declared_variant.get(k)
        }
    return manifest


def write_segment_list_manifest(manifest: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")


def build_ladder_segment_list_manifest(
    segments: list[TimingSegment],
    boundaries: list[AssetBoundary],
    decoded_markers: list[dict],
    renditions: list[dict],
    audio_segments: list | None = None,
    audio_media_paths: dict[int, Path | None] | None = None,
) -> dict:
    """A rendition ladder over ONE shared segment timeline (a VOD's variants
    are segment-aligned). `renditions` is `[{"name", "variant" (declared
    codecs/bandwidth/resolution/frame_rate, or None), "media_paths"
    ({index: Path | None})}, ...]`, best variant first -- the first is the
    reference rendition bake.py carries the shared audio on.

    One rendition degenerates to the classic single-rendition shape
    (`segments[].media_file` + top-level `variant`), so a ladder of one stays
    readable by every older consumer. With more, each rendition carries its
    own `media_files` list and the shared `segments` entries carry timing only
    (loop-dee-loop/SCOPE.md §11.2's ladder extension)."""
    if not renditions:
        raise ValueError("A segment-list manifest needs at least one rendition")
    names = [r["name"] for r in renditions]
    if len(set(names)) != len(names):
        raise ValueError(f"Duplicate rendition names: {names}")

    first = renditions[0]
    manifest = build_segment_list_manifest(
        segments, boundaries, decoded_markers, first["media_paths"], first.get("variant"),
        audio_segments, audio_media_paths,
    )
    if len(renditions) == 1:
        return manifest

    for entry in manifest["segments"]:
        del entry["media_file"]
    manifest.pop("variant", None)
    manifest["renditions"] = []
    for rendition in renditions:
        variant = rendition.get("variant") or {}
        manifest["renditions"].append({
            "name": rendition["name"],
            "variant": {
                k: variant[k] for k in ("codecs", "bandwidth", "resolution", "frame_rate") if variant.get(k)
            },
            "media_files": [
                str(rendition["media_paths"][s.index]) if rendition["media_paths"].get(s.index) is not None else None
                for s in segments
            ],
        })
    return manifest
