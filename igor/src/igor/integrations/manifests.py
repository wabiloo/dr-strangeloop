"""VOD manifest-URL sources: a thin wrapper around `grave-robber ingest-url`.

A manifest source is a saved HLS/DASH VOD manifest URL. Unlike an archive
there is no capture to preview: `inspect` fetches just the manifest to show
the rendition ladder, and the import (which downloads every segment of every
chosen rendition) runs as a background job through igor.jobs.runner, spawning
grave-robber's own CLI like archives.py does.

Layout: data/manifests/<name>.json (URL, display name, last import options)
and outputs/manifests/<name>/manifest.json + media/ (the import).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from grave_robber.vod import describe_manifest

from igor import paths
from igor.jobs.runner import Job, runner

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
_DISPLAY_NAME_MAX_LENGTH = 200
_RENDITIONS_RE = re.compile(r"^(all|best|#\d+(,#\d+)*)$")


def validate_manifest_url(url: str) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Manifest URL must be an http(s) URL.")
    return url


def _validate_name(name: str) -> str:
    if not _NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise ValueError(
            "Name must start with a letter or digit and contain only letters, digits, dots, "
            "underscores, or hyphens (max 180 characters)."
        )
    return name


def _name_from_url(url: str) -> str:
    # Hash rather than hostname/filename: many manifests share a CDN host and
    # often the same "master.m3u8" filename, so those collide or mislead.
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def _source_path(name: str) -> Path:
    _validate_name(name)
    return paths.MANIFESTS_DIR / f"{name}.json"


def _import_output_dir(name: str) -> Path:
    return paths.MANIFEST_IMPORTS_DIR / name


def _import_manifest_path(name: str) -> Path:
    return _import_output_dir(name) / "manifest.json"


def _read_source(name: str) -> dict:
    try:
        data = json.loads(_source_path(name).read_text())
    except OSError as exc:
        raise FileNotFoundError(f"No such manifest source: {name}") from exc
    if not isinstance(data, dict) or "manifest_url" not in data:
        raise FileNotFoundError(f"No such manifest source: {name}")
    return data


def _write_source(name: str, data: dict) -> None:
    paths.MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _source_path(name)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def _entry(name: str, data: dict) -> dict:
    display_name = data.get("display_name")
    return {
        "name": name,
        "display_name": display_name if isinstance(display_name, str) and display_name.strip() else name,
        "manifest_url": data["manifest_url"],
        "import_options": data.get("import_options"),
        "import": import_status(name) if _import_manifest_path(name).is_file() else None,
    }


def list_manifests() -> list[dict]:
    paths.MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for path in sorted(paths.MANIFESTS_DIR.glob("*.json")):
        try:
            out.append(_entry(path.stem, _read_source(path.stem)))
        except (FileNotFoundError, ValueError):
            continue  # not one of ours
    return out


def get_manifest(name: str) -> dict:
    return _entry(name, _read_source(name))


def create_manifest(manifest_url: str, name: str | None = None, display_name: str | None = None) -> dict:
    manifest_url = validate_manifest_url(manifest_url)
    name = _validate_name(name) if name else _name_from_url(manifest_url)
    if _source_path(name).exists():
        raise FileExistsError(f"A manifest source named {name!r} already exists.")
    data = {"manifest_url": manifest_url, "created_at": time.time()}
    if display_name and display_name.strip():
        data["display_name"] = _clean_display_name(display_name)
    _write_source(name, data)
    return _entry(name, data)


def _clean_display_name(display_name: str) -> str:
    display_name = display_name.strip()
    if not display_name or len(display_name) > _DISPLAY_NAME_MAX_LENGTH:
        raise ValueError(f"Display name must be 1-{_DISPLAY_NAME_MAX_LENGTH} characters.")
    if any(ord(char) < 32 or ord(char) == 127 for char in display_name):
        raise ValueError("Display name cannot contain control characters.")
    return display_name


def rename_manifest(name: str, display_name: str) -> dict:
    data = _read_source(name)
    data["display_name"] = _clean_display_name(display_name)
    _write_source(name, data)
    return {"name": name, "display_name": data["display_name"]}


def delete_manifest(name: str) -> None:
    """Delete the saved source only. The import output is retained: an existing
    channel config may still use outputs/manifests/<name>/manifest.json."""
    _read_source(name)
    _source_path(name).unlink()


def inspect_manifest(manifest_url: str) -> dict:
    """Fetch just the manifest (no segments) and describe its rendition ladder."""
    return describe_manifest(validate_manifest_url(manifest_url))


def spawn_import_job(
    name: str,
    *,
    renditions: str | None = None,
    audio: bool = True,
    allow_missing_segments: bool = False,
) -> Job:
    """Run `grave-robber ingest-url` for this source as a background job.
    `renditions` is `all`, `best`, or ranked positions like `#1,#3` (the
    `position` values `inspect_manifest` returns)."""
    data = _read_source(name)
    if renditions is not None and not _RENDITIONS_RE.fullmatch(renditions):
        raise ValueError("renditions must be 'all', 'best', or ranked positions like '#1,#3'")
    cmd = paths.grave_robber_python() + [
        "ingest-url", data["manifest_url"], "--output", str(_import_output_dir(name)),
    ]
    if renditions:
        cmd += ["--renditions", renditions]
    if not audio:
        cmd.append("--no-audio")
    if allow_missing_segments:
        cmd.append("--allow-missing-segments")
    data["import_options"] = {
        "renditions": renditions or "all", "audio": audio, "allow_missing_segments": allow_missing_segments,
    }
    _write_source(name, data)
    return runner.spawn("manifest-import", cmd, cwd=paths.REPO_ROOT, channel_name=name)


def import_status(name: str) -> dict:
    _read_source(name)
    manifest_path = _import_manifest_path(name)
    if not manifest_path.is_file():
        return {"exists": False, "manifest_path": None, "summary": None}
    summary = None
    try:
        manifest = json.loads(manifest_path.read_text())
        renditions = manifest.get("renditions") or [{"name": "(single)", "variant": manifest.get("variant") or {}}]
        summary = {
            "segments": len(manifest["segments"]),
            "duration_seconds": sum(s["duration_ticks"] for s in manifest["segments"]) / 90_000,
            "markers": len(manifest.get("markers", [])),
            "audio": bool((manifest.get("audio") or {}).get("separate")),
            "renditions": [
                {"name": r["name"], "bandwidth": (r.get("variant") or {}).get("bandwidth"),
                 "resolution": (r.get("variant") or {}).get("resolution")}
                for r in renditions
            ],
        }
    except (OSError, ValueError, KeyError):
        pass  # unreadable summary must not hide that an import exists
    return {"exists": True, "manifest_path": str(manifest_path), "summary": summary}


def find_manifest_for_source(source_path: str) -> str | None:
    """Reverse lookup for a channel's `outputs/manifests/<name>/manifest.json`
    source path; only returns a link target while the saved source exists."""
    if not source_path:
        return None
    parts = PurePosixPath(source_path.replace("\\", "/")).parts
    if len(parts) < 3 or parts[-3] != "manifests" or parts[-1] != "manifest.json":
        return None
    try:
        _read_source(parts[-2])
    except (FileNotFoundError, ValueError):
        return None
    return parts[-2]
