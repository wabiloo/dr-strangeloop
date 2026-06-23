from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import OutputConfig

logger = logging.getLogger(__name__)

# Bump this whenever the extraction recipe changes in a way that makes old
# cache artifacts incompatible (filters, codec params, stream layout, …) so
# stale entries are not silently reused.
_RECIPE_VERSION = "extract_v1_vonly+aonly"


@dataclass(frozen=True)
class ClipSegments:
    """The two artifacts produced for one timeline clip.

    Video and audio are deliberately kept in SEPARATE files.  See extract.py
    for the full rationale — in short, muxing them together reintroduces a
    video/audio length mismatch at every clip join that breaks strict CFR.
    """
    video: Path   # video-only MPEG-TS segment (IDR at t=0, exact frame count)
    audio: Path   # audio-only file covering the same clip range


def _key(
    source: Path,
    output: OutputConfig,
    inpoint: float,
    outpoint: float,
) -> str:
    """Content-addressed cache key for one extracted clip.

    Keyed on the source file identity (path + mtime + size), the output spec
    that governs normalization (resolution / framerate / gop / bitrate), and
    the exact frame-snapped cut range.  Two clips that cut the same range from
    the same source to the same spec share a cache entry.
    """
    stat = source.stat()
    parts = (
        f"{source.resolve()}|{stat.st_mtime}|{stat.st_size}"
        f"|{output.resolution}|{output.framerate}|{output.gop}|{output.bitrate_kbps}|48000"
        f"|in={inpoint:.6f}|out={outpoint:.6f}|{_RECIPE_VERSION}"
    )
    return hashlib.sha256(parts.encode()).hexdigest()[:24]


def _paths(cache_dir: Path, key: str) -> ClipSegments:
    return ClipSegments(
        video=cache_dir / f"{key}.v.ts",
        audio=cache_dir / f"{key}.a.m4a",
    )


def lookup(
    source: Path,
    output: OutputConfig,
    cache_dir: Path,
    inpoint: float,
    outpoint: float,
) -> Optional[ClipSegments]:
    """Return cached clip segments if BOTH artifacts exist, otherwise None."""
    key = _key(source, output, inpoint, outpoint)
    segs = _paths(cache_dir, key)
    if segs.video.exists() and segs.audio.exists():
        logger.info("Cache hit for %s [%.3f–%.3f] → %s", source.name, inpoint, outpoint, key)
        return segs
    return None


def store(
    video_src: Path,
    audio_src: Path,
    source: Path,
    output: OutputConfig,
    cache_dir: Path,
    inpoint: float,
    outpoint: float,
) -> ClipSegments:
    """Copy freshly-extracted clip segments into the cache. Returns cache paths."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _key(source, output, inpoint, outpoint)
    segs = _paths(cache_dir, key)
    shutil.copy2(video_src, segs.video)
    shutil.copy2(audio_src, segs.audio)
    logger.info("Cached %s [%.3f–%.3f] → %s", source.name, inpoint, outpoint, key)
    return segs
