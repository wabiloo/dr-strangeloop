from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import AdBreakConfig, AssetConfig
from .validate import VideoInfo

logger = logging.getLogger(__name__)


@dataclass
class TimelineEntry:
    source_file: Path
    inpoint: float         # seconds within the source file (frame-snapped)
    outpoint: float        # seconds within the source file (frame-snapped)
    output_start: float    # seconds from start of the output stream
    output_end: float      # seconds from start of the output stream
    ad_break: Optional[AdBreakConfig]
    inpoint_raw: float     # as computed before frame-snapping
    outpoint_raw: float    # as computed before frame-snapping
    # Countdown overlay fields — resolved in build_timeline().
    countdown: Optional[float] = field(default=None)  # window in seconds (already clamped)
    next_label: Optional[str] = field(default=None)   # "ASSET", "AD", or "END"

    @property
    def clip_duration(self) -> float:
        return self.outpoint - self.inpoint

    @property
    def is_ad_break(self) -> bool:
        return self.ad_break is not None


@dataclass
class AdBoundary:
    """A splice point in the output stream."""
    output_time: float       # seconds in the output
    event_id: int
    is_start: bool           # True = splice-out, False = splice-in
    ad_break: AdBreakConfig
    break_duration: float    # seconds (full ad asset duration)


def _snap_inpoint(t: float, framerate: int) -> float:
    """Round inpoint UP to the next frame boundary (never start before requested)."""
    return math.ceil(t * framerate) / framerate


def _snap_outpoint(t: float, framerate: int) -> float:
    """Round outpoint DOWN to the nearest frame boundary (never exceed requested)."""
    return math.floor(t * framerate) / framerate


def build_timeline(
    assets: list[AssetConfig],
    infos: dict[Path, VideoInfo],
    framerate: int = 25,
) -> tuple[list[TimelineEntry], list[AdBoundary]]:
    """Build the ordered clip list and collect ad break boundary timestamps.

    inpoint/outpoint values are snapped to frame boundaries at the target
    framerate to guarantee frame-accurate cuts in the concat demuxer.

    Args:
        assets:     Asset configs (file may have been remapped to normalized path).
        infos:      VideoInfo keyed by the (possibly normalized) asset path.
        framerate:  Target output frame rate (used for frame-boundary snapping).

    Returns:
        entries:    Ordered list of TimelineEntry objects.
        boundaries: Ordered list of AdBoundary splice points.
    """
    entries: list[TimelineEntry] = []
    boundaries: list[AdBoundary] = []
    cursor = 0.0

    for asset in assets:
        info = infos[asset.file]
        file_duration = info.duration

        start_raw = asset.start_seconds() or 0.0
        if asset.duration_seconds() is not None:
            end_raw = start_raw + asset.duration_seconds()
        else:
            end_raw = file_duration

        end_raw = min(end_raw, file_duration)

        # Snap to frame boundaries
        start = _snap_inpoint(start_raw, framerate)
        end = _snap_outpoint(end_raw, framerate)

        # Guard: snapping could make end <= start if the range is sub-frame
        if end <= start:
            end = start + 1.0 / framerate

        if start != start_raw or end != end_raw:
            logger.debug(
                "%s: inpoint %.6f→%.6f  outpoint %.6f→%.6f (snapped to %d fps frame boundaries)",
                asset.file.name, start_raw, start, end_raw, end, framerate,
            )

        clip_dur = end - start

        if clip_dur <= 0:
            raise ValueError(
                f"Asset {asset.file}: computed clip duration is {clip_dur:.6f}s "
                f"(start={start}, end={end}, file_duration={file_duration})"
            )

        entry = TimelineEntry(
            source_file=asset.file,
            inpoint=start,
            outpoint=end,
            output_start=cursor,
            output_end=cursor + clip_dur,
            ad_break=asset.ad_break,
            inpoint_raw=start_raw,
            outpoint_raw=end_raw,
        )
        entries.append(entry)

        if asset.is_ad_break:
            ab = asset.ad_break
            boundaries.append(AdBoundary(
                output_time=cursor,
                event_id=ab.event_id,
                is_start=True,
                ad_break=ab,
                break_duration=clip_dur,
            ))
            boundaries.append(AdBoundary(
                output_time=cursor + clip_dur,
                event_id=ab.event_id,
                is_start=False,
                ad_break=ab,
                break_duration=clip_dur,
            ))

        cursor += clip_dur

    # ── Countdown overlay resolution ──────────────────────────────────────────
    # Done in a second pass so every entry's clip_duration is already known.
    for i, (asset, entry) in enumerate(zip(assets, entries)):
        raw = asset.countdown_seconds()
        if raw is None:
            # No countdown configured — leave defaults (None, None).
            continue

        # Determine next-element label by looking ahead in the asset list.
        if i + 1 >= len(assets):
            label = "END"
        elif assets[i + 1].is_ad_break:
            label = "AD"
        else:
            label = "ASSET"

        # Resolve and clamp the window duration.
        clip_dur = entry.clip_duration
        if raw < 0:
            resolved = clip_dur          # -1 sentinel → full clip
        else:
            resolved = min(raw, clip_dur)

        entry.countdown = resolved
        entry.next_label = label

    return entries, boundaries


def all_forced_keyframe_times(entries: list[TimelineEntry]) -> list[float]:
    """Return the output_start time of every entry (forces IDR at every boundary)."""
    times = sorted({e.output_start for e in entries})
    return times
