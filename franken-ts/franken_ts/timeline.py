from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import (
    AssetConfig,
    MarkerConfig,
    is_instant_segmentation,
    segmentation_end_type_id,
)
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
    # OSD fields — resolved in build_timeline().
    next_asset_id: Optional[str] = field(default=None)  # id of the next real (non-image) asset;
                                                          # the playlist loops, so this wraps
                                                          # around to the start when needed
    covering_spans: list[MarkerConfig] = field(default_factory=list)  # non-instant spans
                                                          # covering this entry, outermost first
    is_adbreak: bool = field(default=False)  # any covering span with a non-"custom" lane
    no_osd: bool = field(default=False)      # copied from AssetConfig.no_osd
    osd_label: Optional[str] = field(default=None)  # copied from AssetConfig.osd_label
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
    segmentation_type_id: int | None = None  # actual Table 23 value for this boundary
    marker_index: int = 0     # stable playlist-local identity; event IDs may duplicate in relaxed mode


def pts_for_boundary(pts_map: dict, boundary: AdBoundary) -> int | None:
    """Look up a boundary by internal marker identity, with compatibility
    for callers/tests that still provide the historical event-ID keyed map."""
    value = pts_map.get(("marker", boundary.marker_index, boundary.is_start))
    if value is None:
        value = pts_map.get((boundary.marker_index, boundary.is_start))
    if value is None:
        value = pts_map.get((boundary.event_id, boundary.is_start))
    return value


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

    # ── Per-entry OSD/fade resolution ───────────────────────────────────────────
    # Done in a second pass so every entry's clip_duration is already known.
    n_assets = len(assets)
    for i, (asset, entry) in enumerate(zip(assets, entries)):
        clip_dur = entry.clip_duration

        # ── Next asset id ──────────────────────────────────────────────────────
        # The playlist loops, so there is always a "next" asset: scan forward,
        # wrapping around to the start, skipping still images (they're
        # invisible to the viewer as a distinct "next" item). Falls back to
        # the source file's stem if the found asset has no id. Only resolves
        # to None if every other asset is a still image.
        next_id = None
        for step in range(1, n_assets):
            j = (i + step) % n_assets
            if not is_image(assets[j].file):
                a = assets[j]
                next_id = a.id if a.id is not None else a.file.stem
                break
        entry.next_asset_id = next_id
        entry.no_osd = asset.no_osd
        entry.osd_label = asset.osd_label

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

    # ── SCTE-35 span coverage (for the scte35_spans/is_adbreak OSD corners) ────
    # Computed once here and threaded into resolve_markers so both the OSD
    # text and the actual splice boundaries agree on the exact same nesting.
    spans = compute_marker_spans(markers or [], entries)
    for i, entry in enumerate(entries):
        covering = spans_covering(i, spans)
        entry.covering_spans = [s.marker for s in covering]
        entry.is_adbreak = any(m.type != "custom" for m in entry.covering_spans)

    boundaries.extend(resolve_markers(markers or [], entries, spans=spans))

    return entries, boundaries


@dataclass
class MarkerSpan:
    """One marker's span, resolved to timeline-entry indices, with its
    nesting depth (0 = top-level/outermost, increasing with nesting)."""
    lo: int
    hi: int
    marker: MarkerConfig
    depth: int


def compute_marker_spans(
    markers: list[MarkerConfig],
    entries: list[TimelineEntry],
) -> list[MarkerSpan]:
    """Resolve each marker's `assets` id list to a (lo, hi) index range over
    `entries`, plus its nesting depth via span containment (see
    `resolve_markers`'s docstring for the containment/parent/sibling
    definitions this mirrors). `Config.validate_markers` already guarantees
    every pair of spans is nested or disjoint, so this is safe without
    re-checking here."""
    if not markers:
        return []

    id_to_index: dict[str, int] = {}
    for i, entry in enumerate(entries):
        if entry.asset_id is not None:
            id_to_index[entry.asset_id] = i

    raw_spans: list[tuple[int, int, MarkerConfig]] = []
    for marker in markers:
        indices = [id_to_index[aid] for aid in marker.assets]
        raw_spans.append((min(indices), max(indices), marker))

    def contains(outer: tuple[int, int, MarkerConfig], inner: tuple[int, int, MarkerConfig]) -> bool:
        lo_o, hi_o, _ = outer
        lo_i, hi_i, _ = inner
        return lo_o <= lo_i and hi_i <= hi_o and (lo_o, hi_o) != (lo_i, hi_i)

    # Immediate parent = smallest span that strictly contains this one.
    parent_of: dict[int, Optional[int]] = {}
    for i, span in enumerate(raw_spans):
        candidates = [j for j, other in enumerate(raw_spans) if j != i and contains(other, span)]
        parent_of[i] = min(candidates, key=lambda j: raw_spans[j][1] - raw_spans[j][0]) if candidates else None

    # Siblings = spans sharing the same immediate parent, ordered by position.
    siblings_by_parent: dict[Optional[int], list[int]] = {}
    for i, p in parent_of.items():
        siblings_by_parent.setdefault(p, []).append(i)
    for group in siblings_by_parent.values():
        group.sort(key=lambda i: raw_spans[i][0])

    def depth_of(i: int) -> int:
        d = 0
        p = parent_of[i]
        while p is not None:
            d += 1
            p = parent_of[p]
        return d

    return [
        MarkerSpan(lo=lo, hi=hi, marker=marker, depth=depth_of(i))
        for i, (lo, hi, marker) in enumerate(raw_spans)
    ]


def spans_covering(index: int, spans: list[MarkerSpan]) -> list[MarkerSpan]:
    """Non-instant spans covering `entries[index]`, outermost (depth 0)
    first -- e.g. for an asset nested Break > PPO > Ad, returns
    [break_span, ppo_span, ad_span] in that order."""
    covering = [
        s for s in spans
        if s.lo <= index <= s.hi and not is_instant_segmentation(s.marker.segmentation)
    ]
    return sorted(covering, key=lambda s: s.depth)


def resolve_markers(
    markers: list[MarkerConfig],
    entries: list[TimelineEntry],
    spans: Optional[list[MarkerSpan]] = None,
) -> list[AdBoundary]:
    """Resolve the flat `markers` list into `AdBoundary` splice points.

    Each marker's own start/end are *derived* from the already-built
    `entries` (via `asset_id` lookup), never taken from anywhere else --
    this is what keeps nested markers frame-accurate: a marker spanning
    `[jingle, ad1, ad2]` always starts exactly where `jingle`'s entry starts
    and ends exactly where `ad2`'s entry ends, however those durations were
    computed upstream (trims, normalization, frame-snapping, ...).

    Containment (not an authored tree) determines nesting -- see
    `compute_marker_spans` for the parent/sibling/segment_num-autofill
    logic. Pass `spans` when the caller (`build_timeline`) has already
    computed it, so the OSD's `covering_spans`/`is_adbreak` and these
    boundaries are guaranteed to agree; otherwise it's computed fresh here
    (e.g. when calling this function directly, as existing tests do).
    """
    if not markers:
        return []

    if spans is None:
        spans = compute_marker_spans(markers, entries)

    boundaries: list[AdBoundary] = []
    for marker_index, span in enumerate(spans):
        lo, hi, marker = span.lo, span.hi, span.marker
        start_time = entries[lo].output_start
        end_time = entries[hi].output_end
        break_duration = end_time - start_time
        boundaries.append(AdBoundary(
            output_time=start_time,
            event_id=marker.event_id,
            is_start=True,
            marker=marker,
            break_duration=break_duration,
            segmentation_type_id=(
                (int(marker.segmentation.type_id, 16)
                 if isinstance(marker.segmentation.type_id, str)
                 else marker.segmentation.type_id)
                if marker.splice_type == "time_signal" and marker.segmentation is not None
                else None
            ),
            marker_index=marker_index,
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
            stop_type_id = None
            if marker.splice_type == "time_signal" and marker.segmentation is not None:
                start_type_id = (
                    int(marker.segmentation.type_id, 16)
                    if isinstance(marker.segmentation.type_id, str)
                    else marker.segmentation.type_id
                )
                stop_type_id = segmentation_end_type_id(start_type_id)
            boundaries.append(AdBoundary(
                output_time=end_time,
                event_id=marker.event_id,
                is_start=False,
                marker=marker,
                break_duration=break_duration,
                segmentation_type_id=stop_type_id,
                marker_index=marker_index,
            ))

    return boundaries


def all_forced_keyframe_times(entries: list[TimelineEntry]) -> list[float]:
    """Return the output_start time of every entry (forces IDR at every boundary)."""
    times = sorted({e.output_start for e in entries})
    return times
