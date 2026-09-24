"""Thin wrapper around franken-ts: playlist discovery/read/write, JSON Schema
for the frontend's visual editor, and build-job dispatch.

franken-ts is a workspace member sharing igor's own venv (see
paths.py), so its Pydantic models are imported directly here -- the
editor's schema is generated from `franken_ts.config.Config` itself
(`model_json_schema()`), not hand-duplicated, so it can never drift from
what franken-ts actually accepts. "Playlist" is igor's user-facing term
for what franken-ts itself calls a "config" (an asset list + ad-break
YAML file) -- the rename stops at this wrapper/the API/UI; franken-ts's
own module and Pydantic model names are unchanged.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import yaml
from franken_ts.config import Config
from franken_ts.timeline import resolve_markers
from franken_ts.validate import validate_inputs

from igor import paths
from igor.jobs.runner import Job, runner


def playlist_schema() -> dict:
    """JSON Schema for franken-ts's top-level YAML config, straight from
    the Pydantic model franken-ts itself validates against."""
    return Config.model_json_schema()


def list_playlists() -> list[dict]:
    """All *.yaml playlists in franken-ts/playlists/, with just enough
    parsed metadata for a list view (name, output path, asset/marker
    counts) -- full content is fetched separately via get_playlist."""
    out = []
    for path in sorted(paths.FRANKEN_TS_PLAYLISTS_DIR.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            out.append({"name": path.stem, "path": str(path), "error": str(exc)})
            continue
        output = raw.get("output", {}) or {}
        assets = raw.get("assets", []) or []
        markers = raw.get("markers", []) or []
        renditions = output.get("renditions") or []
        out.append({
            "name": path.stem,
            "path": str(path),
            "output_file": _resolve_output_path(output.get("file")),
            "output_dir": _resolve_output_path(output.get("dir")),
            "asset_count": len(assets),
            "marker_count": len(markers),
            # Single-rendition playlists (`output.file`) produce exactly one
            # .ts, so they count as 1 rather than 0 -- matches the file/folder
            # icon distinction the playlist list UI already makes.
            "rendition_count": len(renditions) if renditions else (1 if output.get("file") else 0),
        })
    return out


def _resolve_output_path(raw: str | None) -> str | None:
    """A playlist's `output.file`/`output.dir` is authored relative to
    wherever franken-ts is invoked from, which spawn_build_job always sets
    to REPO_ROOT -- but its-a-live's channel bake resolves a channel's
    [input].source_path relative to its *own* directory (its-a-live/), not
    REPO_ROOT (see _local_docker_ops.py's `os.path.abspath(source_path)`
    with cwd=its-a-live/). Returning an absolute path here means whatever
    consumes it (e.g. ChannelNew.vue's playlist-autofill) can't land a
    source_path that resolves against the wrong directory and 404s the
    markers.json sidecar at bake time."""
    if not raw:
        return raw
    p = Path(raw)
    return str(p if p.is_absolute() else (paths.REPO_ROOT / p).resolve())


def get_playlist(name: str) -> dict:
    path = _resolve_path(name)
    return yaml.safe_load(path.read_text()) or {}


def save_playlist(name: str, data: dict) -> str:
    """Validates against franken_ts.config.Config before writing, so a bad
    form submission never reaches disk as an unusable YAML file."""
    Config.model_validate(data)
    path = _resolve_path(name, must_exist=False)
    paths.FRANKEN_TS_PLAYLISTS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return str(path)


def delete_playlist(name: str) -> None:
    _resolve_path(name).unlink()


def duplicate_playlist(name: str, new_name: str) -> str:
    """Copy an existing playlist's YAML verbatim under a new name. Reuses
    `get_playlist`/`save_playlist` (rather than a raw file copy) so the
    copy still goes through Config validation and the same YAML dump
    formatting as any other save -- and so a bogus/corrupt source name
    surfaces the same FileNotFoundError as get_playlist."""
    data = get_playlist(name)
    dest = _resolve_path(new_name, must_exist=False)
    if dest.is_file():
        raise FileExistsError(f"Playlist already exists: {new_name}")
    return save_playlist(new_name, data)


def resolve_markers_preview(name: str, data: dict | None = None) -> dict:
    """Probe the playlist's real source files and resolve every `markers`
    entry to concrete (start_seconds, end_seconds) plus its auto-filled
    `segment_num`/`segments_expected`, for the "Timeline & markers" editor
    to render marker lanes/spans without running the full build (no
    ffmpeg extraction/assembly -- just ffprobe + the same pure-Python
    timeline math the CLI uses).

    `data`, if given, is used instead of the saved-on-disk YAML -- lets the
    editor preview unsaved in-progress edits (new/moved markers) without
    requiring a save round-trip first.

    Raises on the same validation errors a real build would hit (missing
    files, bad durations, etc.) -- callers surface these as 422s so the
    UI can show them next to the timeline instead of only failing on
    save/build.
    """
    cfg = Config.model_validate(data if data is not None else get_playlist(name))

    report, infos = validate_inputs(cfg.assets, cfg.output, normalize=True)
    if report.has_errors:
        raise ValueError("; ".join(report.errors))

    from franken_ts.timeline import build_timeline

    entries, _asset_boundaries = build_timeline(
        cfg.assets, infos, cfg.output.framerate,
        global_slate_image=cfg.slate_image,
        markers=[],  # resolve markers separately below so we can report
                     # per-marker spans, not just the flattened boundary list
    )

    # Real (ffprobe'd) per-asset durations, keyed by asset id -- lets the
    # timeline editor render assets that have no explicit `duration:` in the
    # YAML (very common for ads: "use the whole file") at their true width
    # instead of a placeholder guess.
    asset_durations = {
        e.asset_id: e.clip_duration for e in entries if e.asset_id is not None
    }

    marker_boundaries = resolve_markers(cfg.markers, entries)
    by_event: dict[int, dict[bool, float]] = {}
    for b in marker_boundaries:
        by_event.setdefault(b.event_id, {})[b.is_start] = b.output_time

    markers_out = []
    for marker in cfg.markers:
        span = by_event.get(marker.event_id, {})
        entry = {
            "event_id": marker.event_id,
            "type": marker.type,
            "assets": marker.assets,
            "start_seconds": span.get(True),
            "end_seconds": span.get(False),
        }
        if marker.segmentation is not None:
            entry["segment_num"] = marker.segmentation.segment_num
            entry["segments_expected"] = marker.segmentation.segments_expected
        markers_out.append(entry)

    return {
        "markers": markers_out,
        "warnings": report.warnings,
        "asset_durations": asset_durations,
    }


def _resolve_path(name: str, must_exist: bool = True):
    if "/" in name or "\\" in name or name in ("..", "."):
        raise ValueError(f"Invalid playlist name: {name!r}")
    path = paths.FRANKEN_TS_PLAYLISTS_DIR / f"{name}.yaml"
    if must_exist and not path.is_file():
        raise FileNotFoundError(f"No such franken-ts playlist: {name}")
    return path


def preview_status(name: str) -> dict:
    """Whether the preview .mp4 exists and is newer than the playlist YAML
    it was built from -- compares mtimes rather than tracking build state
    client-side, so staleness survives page reloads/other browser tabs/
    other clients editing the same playlist. A save always bumps the
    YAML's mtime, so any preview older than that reflects a since-edited
    (possibly since-saved-again) version of the playlist."""
    playlist_path = _resolve_path(name)
    preview_path = preview_mp4_path(name)
    exists = preview_path.is_file()
    stale = (not exists) or (preview_path.stat().st_mtime < playlist_path.stat().st_mtime)
    return {"exists": exists, "stale": stale}


def output_ts_path(name: str) -> Path:
    """Resolve the assembled TS used for the playlist's freshness status."""
    data = get_playlist(name)
    output = data.get("output", {}) or {}
    if output.get("file"):
        p = Path(output["file"])
        return p if p.is_absolute() else (paths.REPO_ROOT / p)
    if output.get("dir"):
        d = Path(output["dir"])
        d = d if d.is_absolute() else (paths.REPO_ROOT / d)
        renditions = output.get("renditions") or []
        if not renditions:
            raise ValueError(f"Playlist {name!r} has no renditions configured")
        return d / f"{renditions[0]['name']}.ts"
    raise ValueError(f"Playlist {name!r} has no output.file or output.dir configured")


def output_status(name: str) -> dict:
    playlist_path = _resolve_path(name)
    output_path = output_ts_path(name)
    exists = output_path.is_file()
    stale = (not exists) or output_path.stat().st_mtime < playlist_path.stat().st_mtime
    return {"exists": exists, "stale": stale}


def preview_mp4_path(name: str) -> Path:
    """Resolve the on-disk path of the quick 540p preview .mp4 franken-ts
    generates as its last build step (see franken-ts/franken_ts/cli.py),
    for the Assemble tab's player. Mirrors franken-ts's own naming:
    `<output.file>.preview.mp4` for single-rendition, or
    `<output.dir>/preview.mp4` for a multi-rendition ladder (built once,
    from the reference rendition) -- does not check existence, callers
    decide how to handle a missing file (e.g. "not built yet")."""
    data = get_playlist(name)
    output = data.get("output", {}) or {}
    if output.get("file"):
        p = Path(output["file"])
        p = p if p.is_absolute() else (paths.REPO_ROOT / p)
        return p.with_suffix(".preview.mp4")
    if output.get("dir"):
        d = Path(output["dir"])
        d = d if d.is_absolute() else (paths.REPO_ROOT / d)
        return d / "preview.mp4"
    raise ValueError(f"Playlist {name!r} has no output.file or output.dir configured")


def report_html_path(name: str) -> Path:
    """Resolve the verification report produced by ``franken-ts --verify``."""
    data = get_playlist(name)
    output = data.get("output", {}) or {}
    if output.get("file"):
        p = Path(output["file"])
        p = p if p.is_absolute() else (paths.REPO_ROOT / p)
        return p.with_name(p.stem + "_report.html")
    if output.get("dir"):
        d = Path(output["dir"])
        d = d if d.is_absolute() else (paths.REPO_ROOT / d)
        renditions = output.get("renditions") or []
        if not renditions:
            raise ValueError(f"Playlist {name!r} has no renditions configured")
        return d / f"{renditions[0]['name']}_report.html"
    raise ValueError(f"Playlist {name!r} has no output.file or output.dir configured")


def report_status(name: str) -> dict:
    playlist_path = _resolve_path(name)
    output_path = output_ts_path(name)
    report_path = report_html_path(name)
    exists = report_path.is_file()
    # A reassemble replaces the TS without changing the saved playlist YAML.
    # The report verifies that TS, so an older report must not be shown as
    # current merely because the playlist itself has not changed.
    newest_input_mtime = playlist_path.stat().st_mtime
    if output_path.is_file():
        newest_input_mtime = max(newest_input_mtime, output_path.stat().st_mtime)
    stale = (not exists) or report_path.stat().st_mtime < newest_input_mtime
    return {"exists": exists, "stale": stale}


def spawn_build_job(name: str, extra_args: list[str] | None = None) -> Job:
    playlist_path = _resolve_path(name)
    cmd = paths.franken_ts_python() + [str(playlist_path), *(extra_args or [])]
    return runner.spawn("build", cmd, cwd=paths.REPO_ROOT, channel_name=name)


def spawn_report_job(name: str) -> Job:
    playlist_path = _resolve_path(name)
    cmd = paths.franken_ts_python() + [str(playlist_path), "--report-only"]
    return runner.spawn("report", cmd, cwd=paths.REPO_ROOT, channel_name=name)


def find_playlist_for_source(source_path: str) -> str | None:
    """Best-effort reverse lookup: which playlist's output matches an
    its-a-live channel's [input].source_path? Playlist output paths (in
    data/playlists/*.yaml) and a channel's source_path are both
    relative, but resolved against whatever working directory each tool
    was last invoked from -- not guaranteed to be the same one -- so a
    strict path-equality check would false-negative constantly. Matching
    on the final path component (basename) is looser but far more robust
    in practice, since output filenames are chosen to be unique."""
    if not source_path:
        return None
    target = PurePosixPath(source_path.replace("\\", "/")).name
    if not target:
        return None
    for playlist in list_playlists():
        for candidate in (playlist.get("output_file"), playlist.get("output_dir")):
            if candidate and PurePosixPath(str(candidate).replace("\\", "/")).name == target:
                return playlist["name"]
    return None
