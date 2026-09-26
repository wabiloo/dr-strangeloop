from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional
from urllib.parse import urlparse

from .config import AssetConfig
from .utils import check_tool, is_url, run_cmd, source_str

logger = logging.getLogger(__name__)

# Master/media manifest extensions that identify an asset as an HLS or DASH
# stream (as opposed to a flat remote mp4, which ffmpeg already reads
# directly -- see utils.py's is_url/source_str). Matched on the URL's path
# component only, so a signed query string (`...m3u8?token=...`) doesn't
# defeat the check.
_STREAM_SUFFIXES = {".m3u8", ".mpd"}

# yt-dlp live_status values that mean "not a fixed-duration asset" -- the
# rest of franken-ts (trimming, timeline math, caching) assumes every asset
# has a known, unchanging duration, so an open/live manifest can never be
# treated as one. "was_live" (a finished live stream now served as a closed
# VOD recording) is fine; it has a real, fixed duration.
_LIVE_STATUSES = {"is_live", "is_upcoming", "post_live"}


class StreamAssetError(RuntimeError):
    """Raised when an asset's `file` is an HLS/DASH manifest that cannot be
    resolved to a fixed local asset (live manifest, yt-dlp failure, etc.)."""


@dataclass(frozen=True)
class ResolvedStreamAsset:
    url: str
    local_path: Path
    cached: bool


def is_stream_url(file: Path) -> bool:
    """True if `file` is an http(s) URL pointing at an HLS (.m3u8) or DASH
    (.mpd) manifest, as opposed to a flat local/remote mp4 (which ffmpeg
    already reads directly with no extra handling)."""
    if not is_url(file):
        return False
    suffix = PurePosixPath(urlparse(source_str(file)).path).suffix.lower()
    return suffix in _STREAM_SUFFIXES


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:24]


def _probe(url: str) -> dict:
    """Metadata-only yt-dlp call (no download) -- used to confirm the
    manifest is VOD/closed before committing to a full download."""
    result = run_cmd(
        ["yt-dlp", "-J", "--no-warnings", "--no-playlist", url],
        capture=True,
    )
    return json.loads(result.stdout)


def _download(url: str, target: Path) -> None:
    """Download the highest-bitrate video+audio rendition of `url` (an HLS
    or DASH manifest) and mux it into a single local mp4 at `target`. yt-dlp
    does the manifest parsing, rendition selection, segment fetching, and
    muxing in one step -- see franken-ts/AGENTS.md for why this is preferred
    over hand-rolling manifest parsing/segment concatenation."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_target = target.parent / f"{target.stem}.part.mp4"
    if tmp_target.exists():
        tmp_target.unlink()

    run_cmd(
        [
            "yt-dlp",
            "-f", "bv*+ba/b",
            "--merge-output-format", "mp4",
            "--no-warnings",
            "--no-playlist",
            "-o", str(tmp_target),
            url,
        ],
        capture=True,
    )
    if not tmp_target.exists():
        raise StreamAssetError(
            f"yt-dlp reported success but produced no output file for {url}"
        )
    tmp_target.replace(target)


def resolve_stream_asset(url: str, cache_dir: Path) -> ResolvedStreamAsset:
    """Resolve one HLS/DASH manifest URL to a local mp4 file: the full
    highest-bitrate rendition, downloaded once and cached by URL under
    `cache_dir` (a raw-source cache, separate from the existing per-clip
    extraction cache in cache.py -- this one caches the whole downloaded
    asset so repeat builds don't re-fetch it). Trimming (`start`/`duration`)
    is applied downstream exactly as for any other local asset; the whole
    VOD is always fetched, never a partial range.

    Raises StreamAssetError if the manifest is live/open rather than a
    closed VOD manifest, or if yt-dlp fails.
    """
    check_tool("yt-dlp")

    target = cache_dir / f"{_cache_key(url)}.mp4"
    if target.exists():
        logger.info("Stream asset cache hit for %s -> %s", url, target)
        return ResolvedStreamAsset(url=url, local_path=target, cached=True)

    info = _probe(url)
    if info.get("is_live") or info.get("live_status") in _LIVE_STATUSES:
        raise StreamAssetError(
            f"{url}: this manifest is live (open-ended), not VOD -- only "
            "closed/finite HLS or DASH manifests can be used as asset "
            "sources (franken-ts needs a fixed duration to trim/stitch)."
        )

    logger.info("Downloading stream asset %s -> %s", url, target)
    _download(url, target)
    return ResolvedStreamAsset(url=url, local_path=target, cached=False)


def resolve_stream_assets(
    assets: list[AssetConfig], cache_dir: Path
) -> list[ResolvedStreamAsset]:
    """Resolve every HLS/DASH manifest asset in `assets` to a local mp4,
    mutating `AssetConfig.file` in place to point at it. Non-stream assets
    (local files, remote mp4s) are left untouched. Returns one
    ResolvedStreamAsset per stream asset actually resolved, in playlist
    order, for the caller to log."""
    resolved: list[ResolvedStreamAsset] = []
    seen: dict[str, Path] = {}
    for asset in assets:
        if not is_stream_url(asset.file):
            continue
        url = source_str(asset.file)
        if url in seen:
            # Same manifest referenced by more than one asset (e.g. trimmed
            # into several clips) -- resolve once, reuse the local path.
            asset.file = seen[url]
            continue
        result = resolve_stream_asset(url, cache_dir)
        asset.file = result.local_path
        seen[url] = result.local_path
        resolved.append(result)
    return resolved
