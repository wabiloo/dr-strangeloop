from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .config import OsdConfig, OutputConfig, abbreviation_for_marker
from .utils import is_url, source_str

if TYPE_CHECKING:
    from .timeline import TimelineEntry

logger = logging.getLogger(__name__)

# Bump this whenever the extraction recipe changes in a way that makes old
# cache artifacts incompatible (filters, codec params, stream layout, …) so
# stale entries are not silently reused.
_RECIPE_VERSION = "extract_v19_vonly+aonly+osd+loop-time+transition+fade+slate+image"


@dataclass(frozen=True)
class ClipSegments:
    """The two artifacts produced for one timeline clip.

    Video and audio are deliberately kept in SEPARATE files.  See extract.py
    for the full rationale — in short, muxing them together reintroduces a
    video/audio length mismatch at every clip join that breaks strict CFR.
    """
    video: Path   # video-only MPEG-TS segment (IDR at t=0, exact frame count)
    audio: Path   # audio-only file covering the same clip range


def entry_cache_key(entry: TimelineEntry, output: OutputConfig, osd: Optional[OsdConfig]) -> str:
    """Compute the cache key for a TimelineEntry + OutputConfig + OsdConfig triple.

    This is the single source of truth for cache identity.  It is also used
    by the within-run dedup dict in cli.py so that both caches are consistent:
    two entries are identical iff they would produce the same extracted segments.

    Keyed on:
    - Source file identity: resolved path + mtime + size (local files), or
      just the URL string (remote files -- no local stat available; the
      recipe version bump is relied on if the remote content ever changes
      without the URL changing, which is expected to be rare/never for
      these immutable ad-placeholder assets)
    - Output spec: resolution, framerate, gop, bitrate
    - Cut range: frame-snapped inpoint + outpoint
    - OSD: resolved per-entry OSD data (no_osd, is_adbreak, next_asset_id,
      osd_label, covering span abbreviations) plus the full OsdConfig itself
    - Overlay params: fade_in, fade_out
    - Slate image identity: resolved path + mtime + size (if set)
    - Recipe version: invalidates stale cache entries after pipeline changes
    """
    source = entry.source_file
    if is_url(source):
        source_part = f"url:{source_str(source)}"
    else:
        stat = source.stat()
        source_part = f"{source.resolve()}|{stat.st_mtime}|{stat.st_size}"

    fade_part = f"{entry.fade_in}:{entry.fade_out}"

    span_abbrevs = ",".join(abbreviation_for_marker(m) for m in entry.covering_spans)
    loop_time_part = ""
    configured_corners = set(osd.corners.model_dump().values()) if osd is not None else set()
    if "loop_time" in configured_corners:
        # The new whole-playlist clock is rendered into each clip's video,
        # so identical source ranges at different playlist positions are no
        # longer interchangeable cache hits.
        loop_time_part = f":{entry.output_start:.6f}:{entry.loop_duration:.6f}"
    transition_part = ""
    if "transition" in configured_corners:
        transition_part = f":{entry.role or ''}:{entry.output_end == entry.loop_duration}"
    loop_bar_part = ""
    if osd is not None and osd.progress_bar.mode == "loop":
        # Loop mode: every clip bakes in the whole-loop map and its own playhead
        # offset, so any asset/span change anywhere invalidates every clip.
        layout_digest = hashlib.sha256(repr(entry.loop_layout).encode()).hexdigest()[:16]
        loop_bar_part = f":{entry.output_start:.6f}:{entry.loop_duration:.6f}:{layout_digest}"
    osd_entry_part = (
        f"{entry.no_osd}:{entry.is_adbreak}:{entry.next_asset_id}:"
        f"{entry.osd_label}:{span_abbrevs}{loop_time_part}{transition_part}{loop_bar_part}"
    )
    osd_cfg_part = osd.model_dump_json() if osd is not None else "none"
    osd_part = f"{osd_entry_part}:{osd_cfg_part}"

    if entry.slate_image is not None:
        if is_url(entry.slate_image):
            slate_part = f"url:{source_str(entry.slate_image)}"
        else:
            ss = entry.slate_image.stat()
            slate_part = f"{entry.slate_image.resolve()}|{ss.st_mtime}|{ss.st_size}"
    else:
        slate_part = "none"

    parts = (
        f"{source_part}"
        f"|{output.resolution}|{output.framerate}|{output.gop}|{output.bitrate_kbps}|48000"
        f"|in={entry.inpoint:.6f}|out={entry.outpoint:.6f}"
        f"|osd={osd_part}"
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
    osd: Optional[OsdConfig],
    cache_dir: Path,
) -> Optional[ClipSegments]:
    """Return cached clip segments if BOTH artifacts exist, otherwise None."""
    key = entry_cache_key(entry, output, osd)
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
    osd: Optional[OsdConfig],
    cache_dir: Path,
) -> ClipSegments:
    """Copy freshly-extracted clip segments into the cache. Returns cache paths."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = entry_cache_key(entry, output, osd)
    segs = _paths(cache_dir, key)
    shutil.copy2(video_src, segs.video)
    shutil.copy2(audio_src, segs.audio)
    logger.info("Cached %s [%.3f–%.3f] → %s",
                entry.source_file.name, entry.inpoint, entry.outpoint, key)
    return segs
