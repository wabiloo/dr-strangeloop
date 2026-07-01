from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .config import OutputConfig

if TYPE_CHECKING:
    from .timeline import TimelineEntry

logger = logging.getLogger(__name__)

# Bump this whenever the extraction recipe changes in a way that makes old
# cache artifacts incompatible (filters, codec params, stream layout, …) so
# stale entries are not silently reused.
_RECIPE_VERSION = "extract_v5_vonly+aonly+countdown+fade+slate"


@dataclass(frozen=True)
class ClipSegments:
    """The two artifacts produced for one timeline clip.

    Video and audio are deliberately kept in SEPARATE files.  See extract.py
    for the full rationale — in short, muxing them together reintroduces a
    video/audio length mismatch at every clip join that breaks strict CFR.
    """
    video: Path   # video-only MPEG-TS segment (IDR at t=0, exact frame count)
    audio: Path   # audio-only file covering the same clip range


def entry_cache_key(entry: TimelineEntry, output: OutputConfig) -> str:
    """Compute the cache key for a TimelineEntry + OutputConfig pair.

    This is the single source of truth for cache identity.  It is also used
    by the within-run dedup dict in cli.py so that both caches are consistent:
    two entries are identical iff they would produce the same extracted segments.

    Keyed on:
    - Source file identity: resolved path + mtime + size
    - Output spec: resolution, framerate, gop, bitrate
    - Cut range: frame-snapped inpoint + outpoint
    - Overlay params: countdown window, next_label, fade_in, fade_out
    - Slate image identity: resolved path + mtime + size (if set)
    - Recipe version: invalidates stale cache entries after pipeline changes
    """
    source = entry.source_file
    stat = source.stat()

    fade_part = f"{entry.fade_in}:{entry.fade_out}"
    countdown_part = f"{entry.countdown}:{entry.next_label}"

    if entry.slate_image is not None:
        ss = entry.slate_image.stat()
        slate_part = f"{entry.slate_image.resolve()}|{ss.st_mtime}|{ss.st_size}"
    else:
        slate_part = "none"

    parts = (
        f"{source.resolve()}|{stat.st_mtime}|{stat.st_size}"
        f"|{output.resolution}|{output.framerate}|{output.gop}|{output.bitrate_kbps}|48000"
        f"|in={entry.inpoint:.6f}|out={entry.outpoint:.6f}"
        f"|countdown={countdown_part}"
        f"|fade={fade_part}"
        f"|slate={slate_part}"
        f"|{_RECIPE_VERSION}"
    )
    return hashlib.sha256(parts.encode()).hexdigest()[:24]


def _paths(cache_dir: Path, key: str) -> ClipSegments:
    return ClipSegments(
        video=cache_dir / f"{key}.v.ts",
        audio=cache_dir / f"{key}.a.m4a",
    )


def lookup(
    entry: TimelineEntry,
    output: OutputConfig,
    cache_dir: Path,
) -> Optional[ClipSegments]:
    """Return cached clip segments if BOTH artifacts exist, otherwise None."""
    key = entry_cache_key(entry, output)
    segs = _paths(cache_dir, key)
    if segs.video.exists() and segs.audio.exists():
        logger.info("Cache hit for %s [%.3f–%.3f] → %s",
                    entry.source_file.name, entry.inpoint, entry.outpoint, key)
        return segs
    return None


def store(
    video_src: Path,
    audio_src: Path,
    entry: TimelineEntry,
    output: OutputConfig,
    cache_dir: Path,
) -> ClipSegments:
    """Copy freshly-extracted clip segments into the cache. Returns cache paths."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = entry_cache_key(entry, output)
    segs = _paths(cache_dir, key)
    shutil.copy2(video_src, segs.video)
    shutil.copy2(audio_src, segs.audio)
    logger.info("Cached %s [%.3f–%.3f] → %s",
                entry.source_file.name, entry.inpoint, entry.outpoint, key)
    return segs
