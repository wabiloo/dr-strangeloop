from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import AssetConfig, MarkerConfig, is_instant_segmentation
from .utils import is_image
from .validate import VideoInfo

logger = logging.getLogger(__name__)


@dataclass
class TimelineEntry:
    source_file: Path
    inpoint: float         # seconds within the source file (frame-snapped)
    outpoint: float        # seconds within the source file (frame-snapped)
    output_start: float    # seconds from start of the output stream
    output_end: float      # seconds from start of the output stream
    inpoint_raw: float     # as computed before frame-snapping
    outpoint_raw: float    # as computed before frame-snapping
    asset_id: Optional[str] = field(default=None)  # AssetConfig.id, for `markers` resolution
    # Countdown overlay fields — resolved in build_timeline().
    countdown: Optional[float] = field(default=None)  # window in seconds (already clamped)
    next_label: Optional[str] = field(default=None)   # "ASSET", "AD", or "END"
    # Fade fields — resolved and clamped in build_timeline().
    fade_in: Optional[float] = field(default=None)    # seconds, or None
    fade_out: Optional[float] = field(default=None)   # seconds, or None
    # Slate image for cross-dissolve fades (None → fade to/from black).
    slate_image: Optional[Path] = field(default=None)

    @property
    def clip_duration(self) -> float:
        return self.outpoint - self.inpoint


@dataclass
class AdBoundary:
    """A splice point in the output stream."""
    output_time: float       # seconds in the output
    event_id: int
    is_start: bool           # True = splice-out, False = splice-in
    marker: MarkerConfig
    break_duration: float    # seconds (full marker span duration)


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
    global_slate_image: Optional[Path] = None,
    markers: Optional[list[MarkerConfig]] = None,
) -> tuple[list[TimelineEntry], list[AdBoundary]]:
    """Build the ordered clip list and collect ad break boundary timestamps.

    inpoint/outpoint values are snapped to frame boundaries at the target
    framerate to guarantee frame-accurate cuts in the concat demuxer.

    Args:
        assets:             Asset configs (file may have been remapped to normalized path).
        infos:              VideoInfo keyed by the (possibly normalized) asset path.
        framerate:          Target output frame rate (used for frame-boundary snapping).
        global_slate_image: Fallback slate image used when an asset has no per-asset
                            slate_image set.  Per-asset slate_image takes precedence.

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
            inpoint_raw=start_raw,
            outpoint_raw=end_raw,
            asset_id=asset.id,
        )
        entries.append(entry)

        cursor += clip_dur

    # Asset ids covered by a leaf (single-asset) "ad"-type marker -- used
    # below to label the countdown overlay's "next up" element as an ad
    # vs. plain content. Computed once, ahead of the countdown pass, since
    # full marker resolution (incl. segment_num, below) isn't needed for
    # this — just which single assets a "ad" marker points at.
    id_to_index = {e.asset_id: i for i, e in enumerate(entries) if e.asset_id is not None}
    ad_entry_indices: set[int] = set()
    for m in (markers or []):
        indices = [id_to_index[aid] for aid in m.assets if aid in id_to_index]
        if indices and m.type == "ad" and min(indices) == max(indices):
            ad_entry_indices.add(indices[0])

    # ── Countdown overlay resolution ──────────────────────────────────────────
    # Done in a second pass so every entry's clip_duration is already known.
    for i, (asset, entry) in enumerate(zip(assets, entries)):
        clip_dur = entry.clip_duration

        # ── Countdown ──────────────────────────────────────────────────────────
        raw = asset.countdown_seconds()
        if raw is not None:
            # Determine next-element label by looking ahead in the asset list,
            # skipping over any still-image assets (they are invisible to the
            # viewer as a distinct "next" item — we want the first non-image
            # successor instead).
            label = "END"
            for j in range(i + 1, len(assets)):
                if not is_image(assets[j].file):
                    label = "AD" if j in ad_entry_indices else "ASSET"
                    break

            if raw < 0:
                resolved = clip_dur          # -1 sentinel → full clip
            else:
                resolved = min(raw, clip_dur)

            entry.countdown = resolved
            entry.next_label = label

        # ── Fade in / out ──────────────────────────────────────────────────────
        fi = asset.fade_in_seconds()
        fo = asset.fade_out_seconds()

        if fi is not None:
            fi = min(fi, clip_dur)
        if fo is not None:
            fo = min(fo, clip_dur)

        # If both fades together exceed the clip duration, scale them
        # proportionally so they share the available time without overlapping.
        if fi is not None and fo is not None and fi + fo > clip_dur:
            scale = clip_dur / (fi + fo)
            fi *= scale
            fo *= scale

        entry.fade_in = fi
        entry.fade_out = fo
        # Per-asset slate_image takes precedence over the global fallback.
        entry.slate_image = asset.slate_image if asset.slate_image is not None else global_slate_image

    boundaries.extend(resolve_markers(markers or [], entries))

    return entries, boundaries


def resolve_markers(
    markers: list[MarkerConfig],
    entries: list[TimelineEntry],
) -> list[AdBoundary]:
    """Resolve the flat `markers` list into `AdBoundary` splice points.

    Each marker's own start/end are *derived* from the already-built
    `entries` (via `asset_id` lookup), never taken from anywhere else --
    this is what keeps nested markers frame-accurate: a marker spanning
    `[jingle, ad1, ad2]` always starts exactly where `jingle`'s entry starts
    and ends exactly where `ad2`'s entry ends, however those durations were
    computed upstream (trims, normalization, frame-snapping, ...).

    Containment (not an authored tree) determines nesting: a marker's
    "immediate parent" is the smallest other marker span that strictly
    contains it, and "siblings" are markers sharing that same immediate
    parent. `segmentation.segment_num`/`segments_expected` are auto-filled
    from sibling position/count when left unset by the user (explicit
    values always win). `Config.validate_markers` already guarantees every
    pair of spans is either nested or disjoint, so this is safe to compute
    without re-checking here.
    """
    if not markers:
        return []

    id_to_index: dict[str, int] = {}
    for i, entry in enumerate(entries):
        if entry.asset_id is not None:
            id_to_index[entry.asset_id] = i

    # (start_idx, end_idx, marker) per marker, in input order.
    spans: list[tuple[int, int, MarkerConfig]] = []
    for marker in markers:
        indices = [id_to_index[aid] for aid in marker.assets]
        spans.append((min(indices), max(indices), marker))

    def contains(outer: tuple[int, int, MarkerConfig], inner: tuple[int, int, MarkerConfig]) -> bool:
        lo_o, hi_o, _ = outer
        lo_i, hi_i, _ = inner
        return lo_o <= lo_i and hi_i <= hi_o and (lo_o, hi_o) != (lo_i, hi_i)

    # Immediate parent = smallest span that strictly contains this one.
    parent_of: dict[int, Optional[int]] = {}  # span index -> parent span index (or None = top-level)
    for i, span in enumerate(spans):
        candidates = [j for j, other in enumerate(spans) if j != i and contains(other, span)]
        parent_of[i] = min(candidates, key=lambda j: spans[j][1] - spans[j][0]) if candidates else None

    # Siblings = spans sharing the same immediate parent, ordered by position.
    siblings_by_parent: dict[Optional[int], list[int]] = {}
    for i, p in parent_of.items():
        siblings_by_parent.setdefault(p, []).append(i)
    for group in siblings_by_parent.values():
        group.sort(key=lambda i: spans[i][0])

    for group in siblings_by_parent.values():
        n = len(group)
        for position, i in enumerate(group):
            marker = spans[i][2]
            seg = marker.segmentation
            if seg is None:
                continue
            if seg.segment_num is None:
                seg.segment_num = position
            if seg.segments_expected is None:
                seg.segments_expected = n

    boundaries: list[AdBoundary] = []
    for lo, hi, marker in spans:
        start_time = entries[lo].output_start
        end_time = entries[hi].output_end
        break_duration = end_time - start_time
        boundaries.append(AdBoundary(
            output_time=start_time,
            event_id=marker.event_id,
            is_start=True,
            marker=marker,
            break_duration=break_duration,
        ))
        # Instant/standalone segmentation types (see
        # INSTANT_SEGMENTATION_TYPE_IDS) have no defined "end" partner --
        # e.g. 0x13 Program Breakaway is its own distinct type, not "0x12
        # End" -- so only one boundary is emitted, at the marker's span
        # start. Start/End pairs (the common case: break/ppo/ad/etc.) keep
        # emitting both. `splice_insert` with `auto_return` similarly has no
        # stop boundary -- the receiver returns on its own once
        # `break_duration` elapses, so there's no explicit cue-in to inject.
        needs_stop_boundary = (
            not marker.auto_return if marker.splice_type == "splice_insert"
            else not is_instant_segmentation(marker.segmentation)
        )
        if needs_stop_boundary:
            boundaries.append(AdBoundary(
                output_time=end_time,
                event_id=marker.event_id,
                is_start=False,
                marker=marker,
                break_duration=break_duration,
            ))

    return boundaries


def all_forced_keyframe_times(entries: list[TimelineEntry]) -> list[float]:
    """Return the output_start time of every entry (forces IDR at every boundary)."""
    times = sorted({e.output_start for e in entries})
    return times
