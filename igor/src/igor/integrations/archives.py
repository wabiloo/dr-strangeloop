"""Archive-derived loop import: a thin wrapper around `grave-robber` --
archive discovery, per-variant coverage preview, import-job dispatch, and
output freshness -- mirroring playlists.py/franken_ts.py's shape
(grave-robber/SCOPE.md §10).

grave-robber is a workspace member sharing igor's own venv (like
franken-ts), so its coverage-preview functions are imported directly here
for a fast, synchronous wizard preview (no subprocess round-trip) --
exactly like franken_ts.resolve_markers_preview. The actual (potentially
slow: media extraction over a whole archive) import itself still goes
through the same background-job pattern every other long operation in
this console uses (igor.jobs.runner), spawning grave-robber's own CLI as a
subprocess rather than importing pipeline.ingest() in-process.
"""

from __future__ import annotations

import json
from pathlib import Path

from grave_robber.multivariant import find_audio_playlist, is_multivariant_playlist, parse_multivariant_playlist
from grave_robber.availability import build_variant_segments, combine_audio, covered_ranges
from grave_robber.suggest import VariantSegments, suggest_ranges
from grave_robber.coverage import build_dash_variant_coverage, build_hls_variant_coverage
from trace_shrink import open_trace

from igor import paths
from igor.jobs.runner import Job, runner

ARCHIVE_EXTENSIONS = (".har", ".proxymanlogv2", ".log", ".barc", ".zip")


def list_archives() -> list[dict]:
    """Every archive file directly inside ARCHIVES_DIR, with best-effort
    metadata (session duration, detected variant count, approximate marker
    count -- SCOPE.md §10's list view) plus whether a prior import exists
    and is stale. A single bad/huge archive's metadata failure never takes
    down the whole list."""
    paths.ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for path in sorted(paths.ARCHIVES_DIR.iterdir()):
        if path.is_dir() or path.suffix.lower() not in ARCHIVE_EXTENSIONS:
            continue
        entry: dict = {"name": path.stem, "path": str(path), "format": path.suffix.lstrip(".")}
        try:
            entry.update(_archive_summary(path))
        except Exception as exc:  # noqa: BLE001 -- surface as a per-row error, not a 500 for everyone
            entry["error"] = str(exc)
        entry["import"] = (
            import_status(path.stem) if _import_manifest_path(path.stem).is_file() else None
        )
        out.append(entry)
    return out


def _archive_summary(path: Path) -> dict:
    """Cheap metadata: variant count from the entry index (no manifest
    parsing), overall session wall-clock span from request timestamps, and
    an approximate marker count from the FIRST detected variant's LATEST
    snapshot only (a full per-variant, per-snapshot decode is exactly
    grave-robber's own `ingest`/`coverage` work -- too slow to redo on
    every list-view render for every archive)."""
    trace = open_trace(str(path))
    urls = trace.get_abr_manifest_urls()

    request_times = [
        entry.timeline.request_start for entry in trace.entries if entry.timeline.request_start is not None
    ]
    session_duration_seconds = (
        (max(request_times) - min(request_times)).total_seconds() if len(request_times) >= 2 else None
    )

    marker_count = None
    if urls:
        first_url = str(urls[0].url)
        entries = trace.get_entries_for_url(first_url)
        if entries:
            from grave_robber.extract_dash import extract_dash
            from grave_robber.extract_hls import extract_hls

            latest = entries[-1]
            manifest_text = latest.content_bytes.decode("utf-8", errors="replace")
            extractor = extract_hls if urls[0].format == "HLS" else extract_dash
            try:
                _segments, markers, _boundaries = extractor(manifest_text, first_url)
                marker_count = len(markers)
            except Exception:  # noqa: BLE001 -- best-effort metadata only
                marker_count = None

    return {
        "entry_count": len(trace),
        "variant_count": len(urls),
        "session_duration_seconds": session_duration_seconds,
        "marker_count": marker_count,
    }


def _resolve_archive_path(name: str) -> Path:
    if "/" in name or "\\" in name or name in ("..", "."):
        raise ValueError(f"Invalid archive name: {name!r}")
    for ext in ARCHIVE_EXTENSIONS:
        candidate = paths.ARCHIVES_DIR / f"{name}{ext}"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"No such archive: {name}")


def get_coverage(name: str) -> dict:
    """Per-variant wall-clock coverage map (grave-robber/SCOPE.md §8 steps
    1-2) for the import wizard's range-picker -- analogous to
    franken_ts.resolve_markers_preview. Full per-snapshot decode of every
    variant (unlike the cheap `list_archives` summary above) -- this is
    only called when a human opens the wizard for one specific archive."""
    archive_path = _resolve_archive_path(name)
    trace = open_trace(str(archive_path))
    urls = trace.get_abr_manifest_urls()

    variants = []
    hls_segments: list[VariantSegments] = []
    multivariants = []
    for decorated_url in urls:
        manifest_url = str(decorated_url.url)
        entries = trace.get_entries_for_url(manifest_url)
        if entries and decorated_url.format == "HLS":
            latest_text = entries[-1].content_bytes.decode("utf-8", errors="replace")
            if is_multivariant_playlist(latest_text):
                # No segments of its own: report its renditions as info only,
                # never as an importable/coverage variant.
                multivariants.append(
                    {
                        "manifest_url": manifest_url,
                        "renditions": parse_multivariant_playlist(latest_text, manifest_url),
                    }
                )
                continue
        snapshots = [
            (entry.content_bytes.decode("utf-8", errors="replace"), manifest_url) for entry in entries
        ]
        segments: list = []
        if decorated_url.format == "HLS":
            segments = build_variant_segments(trace, manifest_url)
            audio_info = find_audio_playlist(trace, manifest_url) if segments else None
            if audio_info and audio_info["manifest_url"] and trace.get_entries_for_url(audio_info["manifest_url"]):
                segments = combine_audio(segments, build_variant_segments(trace, audio_info["manifest_url"]))
        if segments:
            ranges = covered_ranges(segments)
            hls_segments.append(VariantSegments(manifest_url, segments))
        else:
            builder = (
                build_hls_variant_coverage if decorated_url.format == "HLS" else build_dash_variant_coverage
            )
            ranges = builder(manifest_url, snapshots).covered_ranges
        variants.append(
            {
                "manifest_url": manifest_url,
                "format": decorated_url.format,
                "covered_ranges": [{"start": start.isoformat(), "end": end.isoformat()} for start, end in ranges],
                "segments": [
                    {"start": seg.start.isoformat(), "end": seg.end.isoformat(), "has_media": seg.has_media}
                    for seg in segments
                ],
            }
        )
    return {
        "name": name,
        "variants": variants,
        "multivariants": multivariants,
        "suggestions": suggest_ranges(hls_segments),
        "selection": get_selection(name),
    }


def _selection_path(name: str) -> Path:
    _resolve_archive_path(name)  # validates the name / existence
    return paths.ARCHIVES_DIR / f"{name}.selection.json"


def get_selection(name: str) -> dict | None:
    """The wizard's last saved choice (range, variant, which smart option),
    persisted beside the archive so a page reload restores it."""
    path = _selection_path(name)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save_selection(name: str, selection: dict) -> dict:
    path = _selection_path(name)
    path.write_text(json.dumps(selection, indent=2) + "\n")
    return selection


def _import_output_dir(name: str) -> Path:
    return paths.ARCHIVE_IMPORTS_DIR / name


def _import_manifest_path(name: str) -> Path:
    return _import_output_dir(name) / "manifest.json"


def spawn_import_job(
    name: str,
    manifest_url: str,
    *,
    format: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> Job:
    """Spawn grave-robber's own CLI (`grave-robber ingest`) as a background
    job (same igor.jobs.runner pattern franken_ts.spawn_build_job uses) --
    `manifest_url` is the human-confirmed reference variant (SCOPE.md §8
    step 5), already resolved by the wizard before this is called.

    `allow_missing_segments` (the ChannelNew.vue checkbox, SCOPE.md §10) is
    deliberately NOT a parameter here: grave-robber's own `ingest` always
    writes `media_file: null` for whatever it can't recover (SCOPE.md §1) --
    the flag only ever matters at loop-dee-loop `bake.py` time (its own
    `--allow-missing-segments`), not at import time.
    """
    archive_path = _resolve_archive_path(name)
    output_dir = _import_output_dir(name)
    cmd = paths.grave_robber_python() + [
        "ingest", str(archive_path), manifest_url, "--output", str(output_dir),
    ]
    if format:
        # The wizard/trace API uses "HLS"/"DASH"; `ingest --format` wants lowercase.
        cmd += ["--format", format.lower()]
    if start and end:
        cmd += ["--start", start, "--end", end]
    return runner.spawn("archive-import", cmd, cwd=paths.REPO_ROOT, channel_name=name)


def import_status(name: str) -> dict:
    """Output freshness: does an import exist, and is it stale vs. the
    source archive (mirrors playlists.py's output_status/preview_status
    mtime-comparison convention, or a re-run of the wizard)."""
    manifest_path = _import_manifest_path(name)
    exists = manifest_path.is_file()
    stale = True
    if exists:
        try:
            archive_path = _resolve_archive_path(name)
            stale = manifest_path.stat().st_mtime < archive_path.stat().st_mtime
        except FileNotFoundError:
            stale = True
    return {
        "exists": exists,
        "stale": stale,
        "manifest_path": str(manifest_path) if exists else None,
    }
