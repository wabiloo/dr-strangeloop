"""Local filesystem browsing (for the asset file picker) and media
probing (ffprobe) -- both operate on the igor *backend's*
filesystem/network, not the browser's. "Local files" here means files
reachable from wherever this backend process runs (same machine as
franken-ts/ffmpeg, by design -- see repo AGENTS.md), not a browser
upload; "browse" lists directories on that machine."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

FFPROBE_TIMEOUT_SECONDS = 20


def browse_directory(path: str | None) -> dict:
    """Lists a directory's immediate contents. Defaults to the user's
    home directory if no path (or an invalid one) is given. Video-ish
    files are flagged so the frontend can highlight them, but nothing is
    filtered out -- browsing needs to reach any file (slates, images)."""
    VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".ts", ".m4v", ".avi", ".webm"}

    base = Path(path).expanduser() if path else Path.home()
    if not base.is_dir():
        base = base.parent if base.parent.is_dir() else Path.home()
    base = base.resolve()

    entries = []
    try:
        children = sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        children = []

    for child in children:
        if child.name.startswith("."):
            continue
        is_dir = child.is_dir()
        entries.append({
            "name": child.name,
            "path": str(child),
            "is_dir": is_dir,
            "is_video": (not is_dir) and child.suffix.lower() in VIDEO_EXTENSIONS,
        })

    parent = str(base.parent) if base.parent != base else None
    return {"path": str(base), "parent": parent, "entries": entries}


def probe_media(path_or_url: str) -> dict:
    """Runs ffprobe against a local path or an http(s) URL (ffprobe
    supports remote URLs natively) and extracts duration + the first
    video stream's resolution/codec/frame rate, if present."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", path_or_url,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffprobe not found on PATH -- required to probe media files.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffprobe timed out after {FFPROBE_TIMEOUT_SECONDS}s probing {path_or_url!r}") from exc

    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path_or_url!r}: {result.stderr.strip() or 'unknown error'}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe produced unparseable output for {path_or_url!r}") from exc

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = fmt.get("duration") or (video_stream or {}).get("duration")

    frame_rate = None
    if video_stream and video_stream.get("r_frame_rate"):
        num, _, den = video_stream["r_frame_rate"].partition("/")
        try:
            frame_rate = round(int(num) / int(den or 1), 3)
        except (ValueError, ZeroDivisionError):
            frame_rate = None

    return {
        "duration_seconds": float(duration) if duration else None,
        "width": video_stream.get("width") if video_stream else None,
        "height": video_stream.get("height") if video_stream else None,
        "video_codec": video_stream.get("codec_name") if video_stream else None,
        "frame_rate": frame_rate,
        "has_audio": audio_stream is not None,
        "audio_codec": audio_stream.get("codec_name") if audio_stream else None,
        "format_name": fmt.get("format_name"),
        "size_bytes": int(fmt["size"]) if fmt.get("size") else None,
    }
