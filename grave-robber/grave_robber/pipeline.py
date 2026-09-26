"""Pipeline orchestration (SCOPE.md §4): ties every stage together for the
common case of one already-selected manifest URL (single rendition, or a
pre-picked reference variant after `coverage.py`'s multi-variant filter --
see cli.py's `coverage` subcommand for the multi-variant reporting step)."""

from __future__ import annotations

from pathlib import Path

from trace_shrink import Format, Trace, open_trace

from .boundaries import merge_timeline_snapshots, select_loop_boundary
from .extract_dash import extract_dash
from .extract_hls import extract_hls
from .manifest_writer import build_segment_list_manifest, write_segment_list_manifest
from .media_extract import extract_media_for_segments
from .models import AssetBoundary, RawMarker, TimingSegment
from .scte35_decode import decode_marker


def extract_snapshots(
    trace: Trace, manifest_url: str, fmt: Format
) -> list[tuple[list[TimingSegment], list[RawMarker], list[AssetBoundary]]]:
    """Extract every manifest snapshot for `manifest_url` (chronological
    order, per `trace_shrink.ManifestStream`'s own sort) via the format-
    appropriate extractor."""
    manifest_stream = trace.get_manifest_stream(manifest_url)
    extractor = extract_hls if fmt == Format.HLS else extract_dash

    snapshots = []
    for entry in manifest_stream:
        manifest_text = entry.content_bytes.decode("utf-8", errors="replace")
        snapshots.append(extractor(manifest_text, str(entry.request.url)))
    return snapshots


def ingest(
    archive_path: str | Path,
    manifest_url: str,
    output_dir: str | Path,
    *,
    format: Format | None = None,
    media_dir: str | Path | None = None,
) -> dict:
    """Run the full pipeline (SCOPE.md §4) for one manifest URL: parse the
    archive, extract + merge every snapshot into one timeline, decode
    every marker, extract whatever media the archive happens to contain,
    and write the segment-list manifest loop-dee-loop's bake.py consumes.

    Returns the manifest dict that was written (also readable back from
    `output_dir/manifest.json`).
    """
    trace = open_trace(str(archive_path))

    if format is None:
        entries = trace.get_entries_for_url(manifest_url)
        if not entries:
            raise ValueError(f"No entries found for manifest URL: {manifest_url}")
        format = Format.from_url_or_mime_type(entries[0].response.mime_type, entries[0].request.url)
        if format is None:
            raise ValueError(
                f"Could not detect HLS/DASH format for {manifest_url} -- pass --format explicitly."
            )

    snapshots = extract_snapshots(trace, manifest_url, format)
    if not snapshots:
        raise ValueError(f"No manifest snapshots found for {manifest_url}")

    segments, raw_markers, asset_boundaries = merge_timeline_snapshots(snapshots)
    segments = select_loop_boundary(segments)

    decoded_markers = [decode_marker(marker) for marker in raw_markers]

    output_dir = Path(output_dir)
    media_dir = Path(media_dir) if media_dir is not None else output_dir / "media"
    media_paths = extract_media_for_segments(trace, segments, media_dir)

    manifest = build_segment_list_manifest(segments, asset_boundaries, decoded_markers, media_paths)
    write_segment_list_manifest(manifest, output_dir / "manifest.json")
    return manifest
