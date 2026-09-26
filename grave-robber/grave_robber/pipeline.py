"""Pipeline orchestration (SCOPE.md §4): ties every stage together for the
common case of one already-selected manifest URL (single rendition, or a
pre-picked reference variant after `coverage.py`'s multi-variant filter --
see cli.py's `coverage` subcommand for the multi-variant reporting step)."""

from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path

from trace_shrink import Format, Trace, open_trace

from .boundaries import merge_timeline_snapshots, select_loop_boundary, trim_timeline
from .extract_dash import extract_dash
from .extract_hls import extract_hls
from .manifest_writer import build_segment_list_manifest, write_segment_list_manifest
from .media_extract import extract_media_for_segments
from .models import AssetBoundary, RawMarker, TimingSegment
from .audio import align_audio_segments
from .multivariant import find_audio_playlist, find_declared_variant
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
    start: _dt.datetime | None = None,
    end: _dt.datetime | None = None,
    audio_manifest_url: str | None = None,
    audio: bool = True,
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
    if not segments:
        raise ValueError(
            f"{manifest_url} yielded no media segments -- if it is a multivariant "
            "playlist, pick one of its variant (media) playlists instead."
        )
    if start is not None and end is not None:
        if format == Format.HLS:
            segments, raw_markers, asset_boundaries = trim_timeline(
                segments, raw_markers, asset_boundaries, start, end
            )
            if not segments:
                raise ValueError(f"No segments lie fully inside {start.isoformat()} -> {end.isoformat()}")
        else:
            logging.getLogger(__name__).warning("Range trimming is HLS-only; importing the full span")
    segments = select_loop_boundary(segments)

    decoded_markers = [decode_marker(marker) for marker in raw_markers]

    output_dir = Path(output_dir)
    media_dir = Path(media_dir) if media_dir is not None else output_dir / "media"
    media_paths = extract_media_for_segments(trace, segments, media_dir)

    declared_variant = find_declared_variant(trace, manifest_url) if format == Format.HLS else None

    # Audio delivered as its own playlist: align its segments to the (trimmed)
    # video segments. Muxed audio needs nothing here -- bake.py detects it.
    aligned_audio = audio_media_paths = None
    if audio and format == Format.HLS:
        if audio_manifest_url is None:
            declared_audio = find_audio_playlist(trace, manifest_url)
            audio_manifest_url = declared_audio["manifest_url"] if declared_audio else None
        if audio_manifest_url and not trace.get_entries_for_url(audio_manifest_url):
            logging.getLogger(__name__).warning(
                "Audio playlist %s is declared but was never captured in the archive; importing without audio",
                audio_manifest_url,
            )
            audio_manifest_url = None
        if audio_manifest_url:
            audio_snapshots = extract_snapshots(trace, audio_manifest_url, Format.HLS)
            audio_segments, _, _ = merge_timeline_snapshots(audio_snapshots)
            aligned_audio = align_audio_segments(segments, audio_segments)
            audio_media_paths = extract_media_for_segments(
                trace,
                [a for a in aligned_audio if a is not None],
                media_dir,
                filename_template="audio_{index:06d}.bin",
            )
    manifest = build_segment_list_manifest(
        segments, asset_boundaries, decoded_markers, media_paths, declared_variant,
        aligned_audio, audio_media_paths,
    )
    write_segment_list_manifest(manifest, output_dir / "manifest.json")
    return manifest
