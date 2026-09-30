"""Serve phase entrypoint (SCOPE.md §4.2). Long-running, stateless per
request -- no in-memory "current position" variable that a timer advances.

    LINT / CODE-REVIEW CHECKLIST ITEM (SCOPE.md §5):
    No persisted or accumulated timing state in this module may be a Python
    `float`. Every persisted tick value is an `int`. Floats appear only as a
    final, one-shot conversion for display/serialization of a single
    response (e.g. an ISO8601 timestamp string) -- never fed back into a
    subsequent computation.

Every request independently derives the current position from `now()`
versus a fixed channel epoch via loop_math.compute_loop_position. Segment
bytes are served completely unmodified (same bytes every loop); only
manifest text differs per request.

Multi-rendition (ABR ladder): `loop_descriptor.json` (v2) carries a list of
`video_renditions`, each independently baked (bake.py) but sharing the same
`segments_per_loop` count and the same marker/timeline data. Exactly one
rendition also carries audio (see bake.py's `include_audio`) -- audio is
never duplicated per video rendition.
"""

from __future__ import annotations

import argparse
import bisect
import copy
import dataclasses
import datetime as _dt
import json
import logging
import math
import time
from collections.abc import Callable
from pathlib import Path

from urllib.parse import urlencode

from flask import Flask, Response, abort, request, send_file

import cmaf
import continuity
from timeshift import TimeWindow, TimeshiftConfig, TimeshiftError, global_index_at, parse_bool, parse_instant_ticks, resolve_window
from loop_math import (
    compute_loop_position,
    global_segment_number,
    program_date_time_ticks,
    segment_index_for_position,
    ticks_to_wall_clock_seconds,
)
from scte35_signaling import (
    SCTE35_XML_NAMESPACE,
    SignalingMarker,
    build_cue_breaks,
    build_cue_in_tag,
    build_cue_out_cont_tag,
    build_cue_out_tag,
    build_daterange_tags,
    build_event_id_map,
    build_grouped_daterange_tags,
    build_scte35_full_xml,
    is_instant_segmentation,
    is_out_marker,
    markers_to_signaling,
    reencode_event_ids,
)

logger = logging.getLogger(__name__)


def _marker_covers_segment(marker: dict, seg_start_ticks: int, seg_end_ticks: int) -> bool:
    """Whether `marker` should be signaled (EXT-X-DATERANGE / DASH <Event>)
    on a segment spanning [seg_start_ticks, seg_end_ticks).

    A CUE-OUT marker with a `segmentation_duration_ticks` represents an
    active *interval* -- [pts_time_ticks, pts_time_ticks + duration) -- not
    just its own single starting instant. It must keep being signaled on
    every segment that interval overlaps, for as long as any such segment
    remains in the DVR window; otherwise a player that joins mid-break, or
    whose playlist/manifest reload lands after the break's *first* segment
    has already scrolled out of the window, never sees any signal that
    it's mid-ad-break at all, even though later segments of that same
    break are still being served.

    A CUE-IN marker (or any marker with no duration) is instead a single
    point-in-time signal -- it only ever belongs to the one segment whose
    window contains its own `pts_time_ticks`, regardless of any duration
    field it happens to carry (that field describes the segmentation
    interval that just elapsed, not an instruction to keep signaling
    forward from here).

    Standalone/instant markers (`is_instant_segmentation`, e.g. 0x02 Call
    Ad Server) are always point-in-time too, even though their even
    `segmentation_type_id` would otherwise satisfy `is_out_marker` -- there
    is no matching CUE-IN that will ever close them, so they must never be
    treated as an open forward-looking interval.
    """
    start = marker["pts_time_ticks"]
    duration = marker.get("segmentation_duration_ticks")
    if duration and is_out_marker(marker) and not is_instant_segmentation(marker):
        end = start + duration
        return seg_start_ticks < end and start < seg_end_ticks
    return seg_start_ticks <= start < seg_end_ticks


def _asset_ids_starting_in_segment(
    asset_boundaries: list[dict], seg_start_ticks: int, seg_end_ticks: int
) -> list[str]:
    """Asset ids (in timeline order) whose `start_ticks` falls inside
    [seg_start_ticks, seg_end_ticks) -- i.e. every new asset that begins
    somewhere within this one segment. Unlike marker placement, this is a
    plain point-in-interval test: an asset boundary is a single instant
    (where the new asset's content starts), never a signaled interval."""
    return [
        b["asset_id"] for b in asset_boundaries
        if seg_start_ticks <= b["start_ticks"] < seg_end_ticks
    ]


def _remap_signaling_markers(
    signaling_markers: list[SignalingMarker], all_markers: list[dict], loop_number: int
) -> list[SignalingMarker]:
    """[markers].increment_event_ids: return `signaling_markers` with their
    `event_id`/`splice_command_b64` remapped to this loop iteration's
    incremented ids (see scte35_signaling.compute_incremented_event_id).

    The id map is built from `all_markers` (every marker in the package),
    not just `signaling_markers` (the ones being rendered right now) --
    a shared multi-descriptor SCTE-35 message can carry OTHER markers'
    event ids too (see bake.py's decode_embedded_scte35 docstring), and
    those must be remapped in lockstep for the re-encoded bytes to stay
    internally consistent regardless of which marker triggered rendering.
    """
    id_map = build_event_id_map(all_markers, loop_number)
    return [
        dataclasses.replace(
            m,
            event_id=id_map[m.event_id],
            splice_command_b64=reencode_event_ids(m.splice_command_b64, id_map),
        )
        for m in signaling_markers
    ]


def _numeric_segment_index(path: Path) -> int:
    stem = path.stem
    tail = stem.rsplit("_", 1)[-1]
    return int(tail)


def compute_asset_boundary_set(asset_boundaries: list[int]) -> set[int]:
    """Every loop always has at least one real asset-boundary discontinuity
    at local index 0 -- the loop wrap itself (grave-robber/SCOPE.md §6.3:
    "the loop-wrap boundary is always real") -- whether or not the baked
    package's own ledger happened to mark index 0 explicitly. Internal
    boundaries (grave-robber/SCOPE.md §6.1) are every other declared index.
    """
    return set(asset_boundaries) | {0}


def compute_discontinuity_sequence(
    global_index: int, segments_per_loop: int, boundaries: set[int]
) -> int:
    """Generalizes the original "one discontinuity per loop wrap" counter
    (which was simply `global_index // segments_per_loop`) to also count
    internal asset-boundary discontinuities (grave-robber/SCOPE.md §6.1).

    With `boundaries == {0}` (the normal franken-ts-authored case, no
    internal joins), this reduces exactly to the original formula -- see
    tests/test_asset_boundary_discontinuity.py.
    """
    sorted_boundaries = sorted(boundaries)
    k = len(sorted_boundaries)
    loop_number, local_index = divmod(global_index, segments_per_loop)
    # Number of boundary points <= local_index, i.e. how many of this
    # loop's own discontinuities have been "reached" by this segment
    # (inclusive -- a segment sitting exactly on a boundary counts as
    # having just crossed it, same convention the original loop-only
    # formula used for local_index 0).
    local_rank = sum(1 for b in sorted_boundaries if b <= local_index)
    return loop_number * k + local_rank - 1


def compute_declared_offset_ticks_by_local_index(
    segments_per_loop: int,
    boundaries: set[int],
    gap_ticks_by_index: dict[int, int],
) -> list[int]:
    """The declared-position accumulator (grave-robber/SCOPE.md §6.2):
    `declared_offset_ticks[i]` is the sum of every `gap_ticks` crossed by
    asset boundaries at or before local index `i`, reset every loop
    iteration (declared position is always loop-relative, computed once
    here and combined with `loop_number * total_loop_duration_ticks`
    exactly like the existing serving-position formula --
    `loop_math.program_date_time_ticks` needs no change, see SCOPE.md §6.2).

    Zero for every index when `gap_ticks_by_index` is empty (the normal,
    non-archive-derived bake path) -- PDT/Period-start then reduce to
    today's plain serving-position formula unchanged.
    """
    offsets = []
    running = 0
    for i in range(segments_per_loop):
        if i in boundaries:
            running += gap_ticks_by_index.get(i, 0)
        offsets.append(running)
    return offsets


class VideoRendition:
    """Read-only view over one rendition's segments within a loop package
    (`<package_dir>/segments/<name>/`). At most one rendition also carries
    the shared audio track (see `has_audio`)."""

    def __init__(self, package_dir: Path, rendition: dict):
        self.name: str = rendition["name"]
        self.sparse: bool = bool(rendition.get("sparse", False))
        # Sparse packages baked as proper CMAF carry one init per span
        # (output period / discontinuity): `init_files[k]` serves segments from
        # `init_span_starts[k]` up to the next span start (None = no media in
        # that span, so its init 404s like its segments). Older sparse
        # packages (no key) have self-initializing segments instead.
        self.init_span_starts: list[int] = [int(i) for i in rendition.get("init_span_starts") or []]
        self.init_files: list[str | None] = list(rendition.get("init_files") or [])
        self.shared_init: bool = bool(self.init_files)
        self.video_variant: dict = rendition["video_variant"]

        self.segment_boundary_ticks: list[int] = [
            int(t) for t in rendition["segment_boundary_ticks"]
        ]
        self.segments_dir = package_dir / "segments" / self.name

        if self.sparse:
            # SCOPE.md §11: segment-list ('sparse') input mode. Segments
            # were remuxed one-by-one into standalone, self-initializing
            # fragments named by their declared ledger index (bake.py's
            # bake_segment_list) -- never a contiguous glob-and-sort, since
            # a media_file:null entry leaves a real hole in the index
            # sequence, not just a shorter list. `segment_present` (also
            # from loop_descriptor.json) tells us which indices to expect a
            # file for; a None entry here is exactly what serve.py's 404
            # guard (segment_path_for_index) checks for.
            self.video_track_id = None
            present_flags = rendition.get("segment_present")
            if present_flags is None or len(present_flags) != len(self.segment_boundary_ticks):
                raise RuntimeError(
                    f"Rendition '{self.name}': sparse package must declare "
                    f"'segment_present' with one entry per segment boundary "
                    f"tick -- package is inconsistent, refusing to serve."
                )
            self.segment_files: list[Path | None] = [
                (self.segments_dir / f"seg_{i:06d}.m4s") if present else None
                for i, present in enumerate(present_flags)
            ]
            for i, (path, present) in enumerate(zip(self.segment_files, present_flags)):
                if present and not path.exists():
                    raise RuntimeError(
                        f"Rendition '{self.name}': loop_descriptor.json "
                        f"declares segment {i} present but {path} does not "
                        f"exist on disk -- package is inconsistent, "
                        f"refusing to serve."
                    )
        else:
            self.video_track_id = int(rendition["video_track_id"])
            self.segment_files = sorted(
                self.segments_dir.glob(f"*track{self.video_track_id}_*.m4s"),
                key=_numeric_segment_index,
            )
            if not self.segment_files:
                raise RuntimeError(f"No segment files found in {self.segments_dir}")
            if len(self.segment_boundary_ticks) != len(self.segment_files):
                raise RuntimeError(
                    f"Rendition '{self.name}': loop_descriptor.json declares "
                    f"{len(self.segment_boundary_ticks)} segment boundary "
                    f"tick(s) but {len(self.segment_files)} physical segment "
                    f"file(s) were found on disk -- package is inconsistent, "
                    f"refusing to serve."
                )

        self.audio_track_id: int | None = rendition.get("audio_track_id")
        self.audio_variant: dict | None = rendition.get("audio_variant")
        self.audio_segment_files: list[Path] = []
        self.audio_segment_boundary_ticks: list[int] = []
        self._audio_timescale: int | None = None
        # Sparse (archive) audio: one audio init per span, per-index files that
        # may be missing (404) independently of the video's.
        self.audio_sparse: bool = self.sparse and bool(rendition.get("audio_sparse"))
        self.audio_init_files: list[str | None] = []
        if self.audio_sparse:
            self.audio_init_files = list(rendition["audio_init_files"])
            audio_present = rendition["audio_segment_present"]
            self.audio_segment_boundary_ticks = [int(t) for t in rendition["audio_segment_boundary_ticks"]]
            if len(audio_present) != len(self.segment_boundary_ticks) or len(
                self.audio_segment_boundary_ticks
            ) != len(self.segment_boundary_ticks):
                raise RuntimeError(
                    f"Rendition '{self.name}': sparse audio must declare one entry per video "
                    f"segment -- package is inconsistent, refusing to serve."
                )
            self.audio_segment_files = [
                (self.segments_dir / f"seg_a_{i:06d}.m4s") if present else None
                for i, present in enumerate(audio_present)
            ]
        if self.audio_track_id is not None:
            self.audio_segment_files = sorted(
                self.segments_dir.glob(f"*track{self.audio_track_id}_*.m4s"),
                key=_numeric_segment_index,
            )
            self.audio_segment_boundary_ticks = [
                int(t) for t in rendition.get("audio_segment_boundary_ticks") or []
            ]
            if len(self.audio_segment_files) != len(self.segment_files):
                raise RuntimeError(
                    f"Rendition '{self.name}': audio track has "
                    f"{len(self.audio_segment_files)} segment file(s) but "
                    f"video track has {len(self.segment_files)} -- must "
                    f"match 1:1. Package inconsistent, refusing to serve."
                )
            if len(self.audio_segment_boundary_ticks) != len(self.audio_segment_files):
                raise RuntimeError(
                    f"Rendition '{self.name}': loop_descriptor.json declares "
                    f"{len(self.audio_segment_boundary_ticks)} audio segment "
                    f"boundary tick(s) but {len(self.audio_segment_files)} "
                    f"physical audio segment file(s) found. Package "
                    f"inconsistent, refusing to serve."
                )

    @property
    def has_audio(self) -> bool:
        return self.audio_track_id is not None or self.audio_sparse

    def audio_timescale(self, default: int) -> int:
        """The audio track's own media timescale (its init's mdhd), which its
        `tfdt`s are written in; `default` (the package timescale) if unreadable."""
        if self._audio_timescale is None:
            found = None
            try:
                if self.audio_sparse:
                    name = next((f for f in self.audio_init_files if f), None)
                    init = self.segments_dir / name if name else None
                else:
                    init = self.audio_init_path()
                found = cmaf.track_timescale(init.read_bytes()) if init else None
            except (OSError, RuntimeError):
                found = None
            self._audio_timescale = found or default
        return self._audio_timescale

    def audio_init_path_for_span(self, span: int) -> Path | None:
        name = self.audio_init_files[span] if 0 <= span < len(self.audio_init_files) else None
        return self.segments_dir / name if name else None

    def init_path(self) -> Path:
        if self.sparse:
            raise RuntimeError(
                f"Rendition '{self.name}' is sparse (self-initializing "
                f"segments, SCOPE.md §11) -- it has no shared init segment "
                f"to serve. Callers must check `.sparse` (or `.self_initializing`, "
                f"same thing) before requesting one."
            )
        candidates = list(self.segments_dir.glob(f"*track{self.video_track_id}_init.mp4"))
        if not candidates:
            raise RuntimeError(f"No init segment found for rendition '{self.name}'")
        return candidates[0]

    def init_path_for_span(self, span: int) -> Path | None:
        """Init segment of output span `span` (sparse CMAF only); None if that
        span has no recovered media."""
        name = self.init_files[span] if 0 <= span < len(self.init_files) else None
        return self.segments_dir / name if name else None

    @property
    def self_initializing(self) -> bool:
        """Alias of `.sparse` for readability at call sites that care about
        the init-segment implication specifically, not the broader
        "may have holes" meaning."""
        return self.sparse and not self.shared_init

    def audio_init_path(self) -> Path:
        assert self.audio_track_id is not None
        candidates = list(self.segments_dir.glob(f"*track{self.audio_track_id}_init.mp4"))
        if not candidates:
            raise RuntimeError(f"No audio init segment found for rendition '{self.name}'")
        return candidates[0]

    def segment_path_for_index(self, index: int) -> Path | None:
        """Returns None for a sparse-mode index with no physical media
        (SCOPE.md §11.3) -- callers (the segment byte-serving route) must
        turn that into a 404, never a crash."""
        return self.segment_files[index % len(self.segment_files)]

    def audio_segment_path_for_index(self, index: int) -> Path | None:
        return self.audio_segment_files[index % len(self.audio_segment_files)]


class LoopPackage:
    """Read-only view over an immutable loop package directory (SCOPE.md §4.1
    step 6). Loaded once at process start; never mutated by request handling.
    Generalizes to N video renditions (an ABR ladder) sharing one audio
    track and one marker/timeline set.
    """

    def __init__(self, package_dir: Path):
        self.package_dir = package_dir
        descriptor_path = package_dir / "loop_descriptor.json"
        with descriptor_path.open("r", encoding="utf-8") as f:
            self.descriptor: dict = json.load(f)

        version = int(self.descriptor.get("version", 1))
        if version < 2:
            raise RuntimeError(
                f"loop_descriptor.json version {version} is too old (this "
                f"serve.py requires version >= 2, with a 'video_renditions' "
                f"list) -- rebake with the current bake.py."
            )

        self.timescale: int = int(self.descriptor["timescale"])
        self.total_loop_duration_ticks: int = int(
            self.descriptor["total_loop_duration_ticks"]
        )
        self.segment_duration_seconds: float = float(
            self.descriptor["segment_duration_seconds"]
        )
        self.markers: list[dict] = self.descriptor["markers"]

        # [markers] settings (its-a-live/AGENTS.md) -- fixed shape of this
        # package's HLS/DASH SCTE-35 signaling, recorded once at bake time
        # (bake.py) and simply read back here; defaults match bake.py's own
        # defaults, for loop packages baked before these settings existed.
        self.daterange_mode: str = self.descriptor.get("daterange_mode", "shared")
        self.cue_tags: str = self.descriptor.get("cue_tags", "none")
        self.increment_event_ids: bool = self.descriptor.get("increment_event_ids", False)
        self.daterange_id_format: str | None = self.descriptor.get("daterange_id_format")
        self.dash_signal_format: str = self.descriptor.get("dash_signal_format", "binary")
        self.dash_descriptor_mode: str = self.descriptor.get("dash_descriptor_mode", "shared")
        # Loop-relative {asset_id, start_ticks} boundaries from franken-ts's
        # .timeline.json (see bake.py's load_asset_boundaries) -- [] for
        # packages baked without one, so HLS/DASH simply author no
        # asset-boundary comments.
        self.asset_boundaries: list[dict] = [
            b for b in self.descriptor.get("asset_boundaries", []) if isinstance(b, dict)
        ]

        # Precomputed once (not per-request): see build_cue_breaks for
        # what this holds and why it's splice_insert-only.
        self.cue_breaks: list[dict] = (
            build_cue_breaks(self.markers) if self.cue_tags in ("alongside", "only") else []
        )

        rendition_dicts = self.descriptor.get("video_renditions") or []
        if not rendition_dicts:
            raise RuntimeError("loop_descriptor.json has no video_renditions")

        self.video_renditions: list[VideoRendition] = [
            VideoRendition(package_dir, r) for r in rendition_dicts
        ]
        # Highest-bandwidth first -- conventional master playlist ordering;
        # not semantically required (players pick by BANDWIDTH value), but
        # a consistent, predictable order regardless of filename/discovery
        # order on disk.
        self.video_renditions.sort(
            key=lambda r: r.video_variant["bandwidth"], reverse=True
        )

        segment_counts = {len(r.segment_files) for r in self.video_renditions}
        if len(segment_counts) != 1:
            raise RuntimeError(
                f"Renditions do not all have the same segment count: "
                f"{[(r.name, len(r.segment_files)) for r in self.video_renditions]} "
                f"-- every rendition in a ladder must share the same "
                f"segments_per_loop. Package inconsistent, refusing to serve."
            )
        self.segments_per_loop = segment_counts.pop()

        audio_renditions = [r for r in self.video_renditions if r.has_audio]
        if len(audio_renditions) > 1:
            raise RuntimeError(
                f"More than one rendition carries audio: "
                f"{[r.name for r in audio_renditions]} -- exactly one "
                f"rendition should carry the shared audio track."
            )
        self.audio_rendition: VideoRendition | None = (
            audio_renditions[0] if audio_renditions else None
        )
        self.hls_format: str = self.descriptor.get("hls_format", "cmaf")
        self.hls_ts_mux_audio: bool = self.descriptor.get("hls_ts_mux_audio", True)
        if self.hls_format not in ("cmaf", "ts"):
            raise RuntimeError(f"Unsupported HLS format: {self.hls_format!r}")
        if self.hls_format == "ts":
            for rendition in self.video_renditions:
                if rendition.sparse:
                    # Sparse-mode TS segments have the same holes as the
                    # CMAF store (SCOPE.md §11.1) -- checked per-index by
                    # the segment-serving route (404 guard) instead of a
                    # blanket count check here.
                    continue
                ts_segments = package_dir / "hls-ts" / rendition.name
                if len(list(ts_segments.glob("*.ts"))) != self.segments_per_loop:
                    raise RuntimeError(f"Missing HLS TS segments for rendition {rendition.name!r}")
            if self.has_audio and not self.hls_ts_mux_audio and not self.audio_rendition.audio_sparse:
                if len(list((package_dir / "hls-ts" / "audio").glob("*.ts"))) != self.segments_per_loop:
                    raise RuntimeError("Missing separate HLS TS audio segments")

        # Reference rendition for boundary-tick-derived quantities that are
        # conceptually shared across the whole ladder (ad-decision timeline,
        # nominal segment durations) -- highest-bandwidth rendition, i.e.
        # video_renditions[0] after the sort above.
        reference = self.video_renditions[0]
        self.segment_boundary_ticks: list[int] = reference.segment_boundary_ticks

        # grave-robber/SCOPE.md §6.1/§6.2: internal asset-boundary
        # discontinuities + the declared-vs-serving position split. Both
        # default to a no-op for a normal (non-archive-derived) package:
        # `asset_boundaries` absent -> only the always-real loop-wrap
        # boundary at local index 0 (compute_asset_boundary_set), and
        # `asset_boundary_gap_ticks` absent -> every declared offset is 0,
        # so PDT/Period-start formulas reduce exactly to today's plain
        # serving-position value.
        # Segment INDICES of internal asset boundaries (sparse/archive bakes). The older
        # spelling stored them under "asset_boundaries", which now holds franken-ts's
        # timeline entries (dicts) -- accept ints there for packages baked before.
        raw_asset_boundaries = list(self.descriptor.get("asset_boundary_indices") or []) + [
            b for b in (self.descriptor.get("asset_boundaries") or []) if isinstance(b, int)
        ]
        self.boundaries: set[int] = compute_asset_boundary_set(raw_asset_boundaries)
        raw_gap_ticks = self.descriptor.get("asset_boundary_gap_ticks") or {}
        self.asset_boundary_gap_ticks: dict[int, int] = {
            int(k): int(v) for k, v in raw_gap_ticks.items()
        }
        self.declared_offset_ticks_by_local_index: list[int] = (
            compute_declared_offset_ticks_by_local_index(
                len(self.segment_boundary_ticks), self.boundaries, self.asset_boundary_gap_ticks
            )
        )

        segment_durations_ticks = [
            (
                self.segment_boundary_ticks[i + 1]
                if i + 1 < len(self.segment_boundary_ticks)
                else self.total_loop_duration_ticks
            )
            - self.segment_boundary_ticks[i]
            for i in range(len(self.segment_boundary_ticks))
        ]
        max_duration_seconds = max(segment_durations_ticks) / self.timescale
        self.max_segment_duration_seconds_rounded_up = int(max_duration_seconds) + 1

    @property
    def has_audio(self) -> bool:
        return self.audio_rendition is not None

    # ── Public playlist names ──────────────────────────────────────────────
    # index.m3u8 (HLS multivariant), stream.mpd (DASH), audio.m3u8 (the single
    # shared audio track), and one media playlist per video rendition:
    # video[_N].m3u8 (video only) or video_audio[_N].m3u8 (audio muxed into the
    # segments, i.e. hls_format=ts with hls_ts_mux_audio). N is the 1-based
    # rendition index, only present when there is more than one rendition.
    INDEX_PLAYLIST = "index.m3u8"
    AUDIO_PLAYLIST = "audio.m3u8"
    DASH_MANIFEST = "stream.mpd"

    @property
    def audio_muxed_in_video(self) -> bool:
        return self.has_audio and self.hls_format == "ts" and self.hls_ts_mux_audio

    def video_playlist_name(self, rendition: VideoRendition) -> str:
        base = "video_audio" if self.audio_muxed_in_video else "video"
        position = self.video_renditions.index(rendition) + 1
        suffix = f"_{position}" if len(self.video_renditions) > 1 else ""
        return f"{base}{suffix}.m3u8"

    def rendition_by_name(self, name: str) -> VideoRendition:
        for r in self.video_renditions:
            if r.name == name:
                return r
        raise KeyError(name)


def _first_present_segment(files: "list[Path | None]", label: str) -> Path:
    """First non-None entry of a (possibly sparse, SCOPE.md §11) segment
    file list -- used by continuity mode's startup tfdt check, which only
    needs one representative real segment, not index 0 specifically (index
    0 itself may be a hole in a sparse package)."""
    for path in files:
        if path is not None:
            return path
    raise RuntimeError(
        f"{label}: every declared segment is a hole (no physical media at "
        f"all) -- cannot verify tfdt version for continuity mode."
    )


def _cmaf_tfdt_version(path: Path) -> int:
    """`tfdt` box version (0 = 32-bit, 1 = 64-bit `baseMediaDecodeTime`) of
    the first movie fragment in a bare CMAF media segment file. Used only by
    continuity mode's startup check (SCOPE.md §12.4) -- a 32-bit tfdt WILL
    eventually overflow on a channel that runs forever and keeps adding
    `loop_number * total_loop_duration_ticks` to it, no matter how large
    that duration is, so continuity mode refuses to start against one."""
    data = path.read_bytes()
    moof = cmaf._find(data, ("moof",))
    tfdt = cmaf._find(data, ("traf", "tfdt"), moof[0] + 8, moof[1]) if moof else None
    if moof is None or tfdt is None:
        raise RuntimeError(f"{path}: no moof/traf/tfdt box found -- not a valid CMAF media fragment")
    return data[tfdt[0] + 8]


class Channel:
    """Fixed channel epoch + loop package. All request handling goes through
    this class's stateless methods -- no attribute here is ever mutated
    after construction.

    `continuous` (SCOPE.md §12): when True, every physical segment's
    internal timestamps are rewritten per request (continuity.py) to a
    genuinely ever-increasing absolute position instead of restarting from
    the same loop-relative values every iteration, so no
    #EXT-X-DISCONTINUITY / new DASH Period is needed at the loop wrap. Only
    supported for a package with no internal asset-boundary discontinuities
    (`boundaries == {0}`) -- i.e. no discontinuity/multiple-Period join
    anywhere in the source, whether that source is a single franken-ts
    encode (always true there) or a grave-robber/archive capture that
    happens to be a single continuous span (SCOPE.md §12.6) -- and CMAF
    fragments baked with a 64-bit (v1) tfdt. Both checked once here, at
    startup, not per request.
    """

    def __init__(
        self,
        package: LoopPackage,
        epoch_ticks: int,
        window_segments: int = 6,
        continuous: bool = False,
    ):
        if not isinstance(epoch_ticks, int):
            raise ValueError("epoch_ticks must be int")
        if window_segments < 1:
            raise ValueError("window_segments must be >= 1")
        self.continuous_error: str | None = None
        if continuous:
            self._validate_continuous(package)
        else:
            # SCOPE.md §13.5: a request may opt in to continuity per-request,
            # so learn once, at startup, whether the package supports it.
            try:
                self._validate_continuous(package)
            except Exception as exc:  # noqa: BLE001 -- any reason means 'unsupported'
                self.continuous_error = str(exc)
        self.package = package
        self.epoch_ticks = epoch_ticks
        # Controls the DVR window / manifest size: how many segments ahead
        # of the live edge are advertised in each manifest response (HLS
        # sliding window, DASH SegmentTimeline + timeShiftBufferDepth).
        # Larger = more seekable-back history for players, larger manifest
        # responses. See --dvr-window-seconds / --window-segments in
        # serve.py's CLI.
        self.window_segments = window_segments
        self.continuous = continuous

    @staticmethod
    def _validate_continuous(package: "LoopPackage") -> None:
        if package.boundaries != {0}:
            raise RuntimeError(
                f"continuity mode (--continuous-timeline) does not support "
                f"internal asset-boundary discontinuities (grave-robber/"
                f"SCOPE.md §6.1) -- only the plain loop-wrap boundary. This "
                f"package declares boundaries={sorted(package.boundaries)} "
                f"(i.e. the source itself has a discontinuity/multiple-"
                f"Period join, not just the loop wrap -- SCOPE.md §12.6)."
            )
        # SCOPE.md §12.6: sparse/self-initializing renditions (grave-robber
        # archive input, §11) are allowed here -- the check above is the
        # real gate ("is there any discontinuity in the source at all"),
        # not `.sparse` itself. A single-span archive capture (boundaries
        # == {0}) is mechanically the same shape as a franken-ts encode as
        # far as continuity is concerned: one span of segments to shift by
        # a constant per loop iteration. Segments with no physical media
        # (a hole, SCOPE.md §11.1) stay a 404 either way -- shift_ticks is
        # never applied to bytes that don't exist.
        for rendition in package.video_renditions:
            representative = _first_present_segment(rendition.segment_files, rendition.name)
            version = _cmaf_tfdt_version(representative)
            if version != 1:
                raise RuntimeError(
                    f"continuity mode requires a 64-bit (v1) tfdt, so the "
                    f"per-loop tick offset (loop_number * "
                    f"total_loop_duration_ticks) never overflows over a "
                    f"long-running channel's lifetime -- rendition "
                    f"'{rendition.name}' was baked with a 32-bit (v0) tfdt. "
                    f"Rebake with a GPAC/ffmpeg invocation that emits v1 "
                    f"tfdt boxes (SCOPE.md §12.4)."
                )
        audio = package.audio_rendition
        if audio is not None and audio.audio_segment_files:
            representative = _first_present_segment(audio.audio_segment_files, "audio")
            version = _cmaf_tfdt_version(representative)
            if version != 1:
                raise RuntimeError(
                    f"continuity mode requires a 64-bit (v1) tfdt for the "
                    f"shared audio track too, same reasoning as the video "
                    f"check above -- rebake with a GPAC/ffmpeg invocation "
                    f"that emits v1 tfdt boxes (SCOPE.md §12.4)."
                )

    def for_mode(self, continuous: bool) -> "Channel":
        """This channel, but serving in the requested continuity mode
        (SCOPE.md §13.5). Channel is immutable after construction, so a
        shallow copy with the flag flipped is a safe per-request variant.
        Raises TimeshiftError if the package can't be served continuously."""
        if continuous == self.continuous:
            return self
        if continuous and self.continuous_error is not None:
            raise TimeshiftError(f"continuous timeline is not supported: {self.continuous_error}")
        variant = copy.copy(self)
        variant.continuous = continuous
        variant.continuous_error = None if continuous else self.continuous_error
        return variant

    def segment_uri(
        self, template: str, global_index: int, local_index: int, window: "TimeWindow | None"
    ) -> str:
        """Segment URI for a manifest entry (SCOPE.md §13.5). `template`
        contains `/seg/{index}`. Default mode: `/seg/<local>`. Continuity:
        `/cseg/<global>`; continuity + HLS-TS + a time-shifted range:
        `/rseg/<origin_loop>/<global>` so timestamps are shifted relative to
        the range origin instead of the epoch (33-bit PTS wrap)."""
        if not self.continuous:
            return template.format(index=local_index)
        if window is not None and template.endswith(".ts"):
            return template.replace("/seg/", f"/rseg/{window.origin_loop}/").format(index=global_index)
        return template.replace("/seg/", "/cseg/").format(index=global_index)

    def resolve_timeshift(
        self,
        start_ticks: int,
        end_ticks: int | None,
        max_span_seconds: int,
        full_loop: bool,
    ) -> TimeWindow:
        pkg = self.package
        return resolve_window(
            start_ticks=start_ticks,
            end_ticks=end_ticks,
            now_ticks=self.now_ticks(),
            epoch_ticks=self.epoch_ticks,
            total_loop_duration_ticks=pkg.total_loop_duration_ticks,
            segment_boundary_ticks=pkg.segment_boundary_ticks,
            segments_per_loop=pkg.segments_per_loop,
            max_span_ticks=max_span_seconds * pkg.timescale,
            full_loop=full_loop,
        )

    def loop_number_and_local_index(self, global_index: int) -> tuple[int, int]:
        """(loop_number, local_index) decoded from a continuity-mode
        segment URL's index, which -- unlike the default mode's plain
        local/physical index -- is the ever-increasing global segment
        number (SCOPE.md §12.2), so the same physical bytes can be shifted
        by the right per-loop offset regardless of which loop iteration a
        given request actually belongs to."""
        return divmod(global_index, self.package.segments_per_loop)

    def continuity_shift_ticks(self, loop_number: int) -> int:
        return loop_number * self.package.total_loop_duration_ticks

    def now_ticks(self) -> int:
        """The only place wall-clock time is sampled. Converted to an
        integer tick count exactly once, at the point of measurement."""
        return round(time.time() * self.package.timescale)

    def current_position(self):
        return compute_loop_position(
            self.now_ticks(), self.epoch_ticks, self.package.total_loop_duration_ticks
        )

    def build_hls_master_playlist(self, query: str = "") -> str:
        """Build the HLS multivariant (master) playlist -- required by the
        HLS spec and by most real players (Safari/hls.js, etc. generally
        expect the entry-point URL to be a multivariant playlist, even for
        a single-rendition/bitrate stream, not a bare media playlist).

        One #EXT-X-STREAM-INF per video rendition, each pointing at its own
        `video[_audio][_N].m3u8` (LoopPackage.video_playlist_name). #EXT-X-STREAM-INF attributes
        (BANDWIDTH/CODECS/RESOLUTION/FRAME-RATE) come from each rendition's
        `video_variant`, the exact codec/resolution/bandwidth GPAC itself
        computed while producing the real segments at bake time (bake.py's
        read_variant_metadata) -- not re-derived or guessed here.

        If the package has an audio track, one #EXT-X-MEDIA audio group is
        declared (shared across all video renditions) and referenced from
        every #EXT-X-STREAM-INF via AUDIO="audio", with CODECS listing both
        the video and audio codec strings (per HLS spec).
        """
        pkg = self.package
        # SCOPE.md §13.5: a time-shifted request's params must ride along on
        # every child playlist URI, else players would fetch the live ones.
        suffix = f"?{query}" if query else ""

        lines = ["#EXTM3U", "#EXT-X-VERSION:6" if pkg.hls_format == "ts" else "#EXT-X-VERSION:7"]

        audio_codecs: list[str] = []
        audio_bandwidth = 0
        separate_audio = pkg.has_audio and (pkg.hls_format == "cmaf" or not pkg.hls_ts_mux_audio)
        if pkg.has_audio and pkg.audio_rendition.audio_variant is not None:
            a = pkg.audio_rendition.audio_variant
            audio_codecs = [a["codecs"]]
            audio_bandwidth = a["bandwidth"]
            if separate_audio:
                lines.append(
                    '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Default",'
                    f'DEFAULT=YES,AUTOSELECT=YES,URI="audio.m3u8{suffix}"'
                )

        for rendition in pkg.video_renditions:
            v = rendition.video_variant
            stream_inf_attrs = [
                f"BANDWIDTH={v['bandwidth'] + audio_bandwidth}",
                f'CODECS="{",".join([v["codecs"], *audio_codecs])}"',
                f"RESOLUTION={v['width']}x{v['height']}",
                f"FRAME-RATE={v['frame_rate']:.3f}",
            ]
            if separate_audio:
                stream_inf_attrs.append('AUDIO="audio"')

            lines.append("#EXT-X-STREAM-INF:" + ",".join(stream_inf_attrs))
            lines.append(pkg.video_playlist_name(rendition) + suffix)

        return "\n".join(lines) + "\n"

    def build_hls_manifest(
        self, rendition_name: str, window_segments: int | None = None, window: TimeWindow | None = None
    ) -> str:
        rendition = self.package.rendition_by_name(rendition_name)
        is_ts = self.package.hls_format == "ts"
        # A sparse-mode rendition's segments are self-initializing (SCOPE.md
        # §11) -- there's no shared init segment to point #EXT-X-MAP at,
        # same as the "ts" format's own no-init convention.
        no_init = is_ts or rendition.self_initializing
        per_span = not is_ts and rendition.shared_init
        return self._build_hls_media_playlist(
            boundary_ticks=rendition.segment_boundary_ticks,
            init_uri=None if no_init or per_span else f"{rendition.name}/init.mp4",
            span_init_uri=(lambda k: f"{rendition.name}/init_{k}.mp4") if per_span else None,
            seg_uri_template=f"{rendition.name}/seg/{{index}}.ts" if is_ts else f"{rendition.name}/seg/{{index}}.m4s",
            window_segments=window_segments,
            window=window,
        )

    def build_hls_audio_manifest(
        self, window_segments: int | None = None, window: TimeWindow | None = None
    ) -> str:
        pkg = self.package
        if not pkg.has_audio:
            raise RuntimeError("This loop package has no audio track to serve.")
        if pkg.hls_format == "ts" and pkg.hls_ts_mux_audio:
            raise RuntimeError("Audio is muxed into the HLS TS video segments.")
        is_ts = pkg.hls_format == "ts"
        return self._build_hls_media_playlist(
            boundary_ticks=pkg.audio_rendition.audio_segment_boundary_ticks,
            init_uri=None if is_ts or pkg.audio_rendition.audio_sparse else "audio/init.mp4",
            span_init_uri=(lambda k: f"audio/init_{k}.mp4") if pkg.audio_rendition.audio_sparse and not is_ts else None,
            seg_uri_template="audio/seg/{index}.ts" if is_ts else "audio/seg/{index}.m4s",
            window_segments=window_segments,
            window=window,
        )

    def _build_hls_media_playlist(
        self,
        *,
        boundary_ticks: list[int],
        init_uri: str | None,
        seg_uri_template: str,
        window_segments: int | None = None,
        span_init_uri: Callable[[int], str] | None = None,
        window: TimeWindow | None = None,
    ) -> str:
        """Shared builder for every video rendition's HLS media playlist and
        for the (single, shared) audio HLS media playlist.

        Segment *indexing*/loop-number sequencing (media_sequence,
        local_index, local_loop_number) is always driven by the reference
        rendition's `segments_per_loop` -- by construction (bake.py) every
        rendition/track has exactly the same segment count per loop, with
        segment i corresponding to the same conceptual time window
        everywhere, even though each rendition/track's own boundary tick
        VALUES were snapped independently. Only `boundary_ticks` (used for
        this playlist's own PROGRAM-DATE-TIME/EXTINF durations) and the
        URIs differ between renditions/audio.

        Marker (DATERANGE) placement is always decided using the
        reference rendition's segment_boundary_ticks (the ad-decision
        timeline), applied to the shared segment index -- so the same
        marker appears at the same segment index in every rendition's
        playlist and in the audio playlist.
        """
        window_segments = window_segments or self.window_segments
        pkg = self.package
        if window is not None:
            # SCOPE.md §13: explicit startover/catchup range instead of the
            # sliding live window ending at "now".
            media_sequence = window.last_global
        else:
            pos = self.current_position()
            seg_index = segment_index_for_position(
                pos.position_in_loop_ticks, pkg.segment_boundary_ticks
            )
            media_sequence = global_segment_number(
                pos.loop_number, seg_index, pkg.segments_per_loop
            )

        # Every physical segment file is reused, byte-for-byte, on every
        # loop iteration (SCOPE.md §4.1 step 6): its internal fMP4
        # timestamps (baseMediaDecodeTime/tfdt) therefore always restart
        # from the same loop-relative values, regardless of which real-world
        # loop iteration is being served. A playlist that presents segment
        # index `segments_per_loop - 1` immediately followed by segment
        # index 0 *without* signaling a discontinuity lies to the player:
        # it claims the two segments are timestamp-continuous when they are
        # not. Browsers' MSE demuxers (observed: Chrome's ChunkDemuxer)
        # detect this as a "RunSegmentParserLoop: stream parsing failed"
        # append failure, which cascades into MediaSource.readyState
        # "ended" and a fatal `mediaSourceRequiresReset` -- i.e. playback
        # dies exactly once per loop, after the first iteration completes.
        # #EXT-X-DISCONTINUITY-SEQUENCE (RFC 8216 §4.3.3.3) identifies the
        # discontinuity sequence of the *first* segment in this window --
        # here, simply its loop number, since every loop boundary is
        # exactly one discontinuity. An #EXT-X-DISCONTINUITY tag (§4.3.2.3)
        # is then emitted immediately before every subsequent segment that
        # starts a new loop iteration (local_index == 0), so the player
        # resets its timestamp-continuity expectations there.
        #
        # `media_sequence` (from segment_index_for_position/
        # global_segment_number above) is the segment CONTAINING "now" --
        # i.e. the live edge, the LAST entry the window should end at. The
        # window itself must span BACKWARD from there (recent history up
        # to and including the live edge), never forward past it: segments
        # after the live edge haven't been "reached" in real time yet, even
        # though the underlying files already exist (every segment is
        # pre-baked -- see LoopPackage docstring), so serving them as if
        # already live lets a client play ahead of true wall-clock time.
        # Clamped to 0 only matters in the first few seconds after this
        # process starts, when fewer than `window_segments` have "aged"
        # yet -- the window is simply smaller than requested until then,
        # same as any real live stream's startup ramp-up.
        first_global_index = (
            window.first_global if window is not None else max(0, media_sequence - window_segments + 1)
        )
        # grave-robber/SCOPE.md §6.1: generalizes the original "one
        # discontinuity per loop wrap" counter (previously just
        # `first_global_index // segments_per_loop`) to also count internal
        # asset-boundary discontinuities -- reduces to exactly that formula
        # when pkg.boundaries == {0} (see compute_discontinuity_sequence).
        # SCOPE.md §12: in continuity mode there is never a discontinuity to
        # report -- every loop wrap's timestamps are rewritten to be
        # genuinely continuous (see the segment byte-serving routes) -- so
        # the sequence header is always 0 rather than computed.
        first_discontinuity_sequence = 0 if self.continuous else compute_discontinuity_sequence(
            first_global_index, pkg.segments_per_loop, pkg.boundaries
        )

        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:6" if init_uri is None and span_init_uri is None else "#EXT-X-VERSION:7",
            f"#EXT-X-TARGETDURATION:{pkg.max_segment_duration_seconds_rounded_up}",
            f"#EXT-X-MEDIA-SEQUENCE:{first_global_index}",
            f"#EXT-X-DISCONTINUITY-SEQUENCE:{first_discontinuity_sequence}",
        ]
        if window is not None:
            lines.append("#EXT-X-PLAYLIST-TYPE:VOD" if window.ended else "#EXT-X-PLAYLIST-TYPE:EVENT")
        sorted_boundaries = sorted(pkg.boundaries)

        def _span_map_line(local_index: int) -> str:
            # One init per output span (discontinuity): the MAP must follow
            # every #EXT-X-DISCONTINUITY and lead the playlist.
            span = bisect.bisect_right(sorted_boundaries, local_index) - 1
            return f'#EXT-X-MAP:URI="{span_init_uri(span)}"'

        if span_init_uri is not None:
            lines.append(_span_map_line(first_global_index % pkg.segments_per_loop))
        elif init_uri is not None:
            lines.append(f'#EXT-X-MAP:URI="{init_uri}"')

        # An EXT-X-DATERANGE describes one point on the presentation
        # timeline; it must appear exactly ONCE per manifest response, on
        # whichever currently-in-window segment is the earliest one its
        # active interval still overlaps -- NOT repeated on every segment
        # that interval spans. As older overlapping segments age out of the
        # window across successive polls, the tag's anchor simply moves
        # forward to whatever is now the earliest still-present overlapping
        # segment (same ID, byte-identical attributes, per RFC 8216
        # 4.4.5.1) -- it disappears entirely only once the very last
        # overlapping segment (i.e. the one containing the marker's own end
        # tick, for a CUE-IN) has scrolled out. Segments are visited below
        # in increasing window order (oldest/earliest first), so a simple
        # "already emitted in this response" set is sufficient to enforce
        # the single-occurrence rule without a separate pre-pass.
        already_emitted_markers: set[tuple[str, object, int]] = set()

        # [markers].cue_tags: which cue_breaks (see LoopPackage.__init__)
        # have already had their opening #EXT-X-CUE-OUT/-CONT emitted in
        # this response -- same per-response, visited-in-increasing-order
        # dedupe pattern as already_emitted_markers above.
        seen_cue_event_ids: set[str] = set()

        # Never emit past `media_sequence` (the live edge) -- when
        # first_global_index was clamped to 0 above (only possible in the
        # first few seconds of this process's life), a plain
        # `range(window_segments)` would overshoot past the live edge and
        # reintroduce future segments, the exact bug being fixed here.
        entries_in_window = media_sequence - first_global_index + 1
        for i in range(entries_in_window):
            global_index = first_global_index + i
            local_index = global_index % pkg.segments_per_loop
            local_loop_number = global_index // pkg.segments_per_loop

            if i > 0 and local_index in pkg.boundaries and not self.continuous:
                # This segment starts a new loop iteration OR an internal
                # asset-boundary join (grave-robber/SCOPE.md §6.1), and
                # isn't the very first entry in the window (whose implicit
                # discontinuity sequence is already covered by the header
                # above) -- signal the timestamp discontinuity here. Never
                # reached in continuity mode (SCOPE.md §12): that mode
                # requires boundaries == {0} (Channel._validate_continuous),
                # and the loop-wrap boundary itself needs no signal there
                # since the served bytes are rewritten to be genuinely
                # continuous across it.
                lines.append("#EXT-X-DISCONTINUITY")
                if span_init_uri is not None:
                    lines.append(_span_map_line(local_index))

            # This playlist's own segment start/end (for PDT/EXTINF).
            segment_start_ticks = boundary_ticks[local_index]
            if local_index + 1 < len(boundary_ticks):
                seg_end_ticks_local = boundary_ticks[local_index + 1]
            else:
                seg_end_ticks_local = pkg.total_loop_duration_ticks

            # Marker placement always decided from the reference rendition's
            # timeline (the ad-decision authority), applied to this same
            # segment index, so every playlist advertises the same marker
            # at the same index even though boundary tick VALUES differ.
            ref_seg_start_ticks = pkg.segment_boundary_ticks[local_index]
            if local_index + 1 < len(pkg.segment_boundary_ticks):
                ref_seg_end_ticks = pkg.segment_boundary_ticks[local_index + 1]
            else:
                ref_seg_end_ticks = pkg.total_loop_duration_ticks

            # Markers whose loop-relative tick falls inside this segment's
            # loop-relative window get their DATERANGE built here, fresh,
            # for *this* request -- never reused from a stale pre-rendered
            # string. START-DATE is computed against this segment's own
            # local_loop_number, so for a marker whose PTS sits exactly at
            # a segment boundary (true by construction here, since GPAC's
            # cue-driven dasher split segments exactly at marker ticks --
            # see bake.py), START-DATE is byte-for-byte identical to this
            # segment's own EXT-X-PROGRAM-DATE-TIME below (both derive from
            # the same absolute tick). A marker further inside a segment
            # (announced in advance of its own segment boundary) instead
            # gets a START-DATE strictly after that segment's PDT and before
            # the next one's, still correctly anchored to real wall-clock
            # time -- never a fixed placeholder epoch.
            #
            # A CUE-OUT's DATERANGE stays "active" (per _marker_covers_segment)
            # across every segment its interval overlaps, but must only be
            # emitted on the FIRST such segment still present in this window
            # -- not repeated on each one (see already_emitted_markers above).
            # Skipped entirely when cue_tags="only" -- no DATERANGE fallback
            # in that mode, #EXT-X-CUE-OUT/-IN below is the only signaling.
            if pkg.cue_tags != "only":
                matching_markers = [
                    m for m in pkg.markers
                    if (
                        m["event_id"], m.get("marker_identity"), m["pts_time_ticks"]
                    ) not in already_emitted_markers
                    and _marker_covers_segment(m, ref_seg_start_ticks, ref_seg_end_ticks)
                ]
                for m in matching_markers:
                    already_emitted_markers.add(
                        (m["event_id"], m.get("marker_identity"), m["pts_time_ticks"])
                    )
                if matching_markers:
                    loop_start_ticks = program_date_time_ticks(
                        local_loop_number, 0, pkg.total_loop_duration_ticks, self.epoch_ticks
                    )
                    loop_start_seconds = ticks_to_wall_clock_seconds(
                        loop_start_ticks, pkg.timescale
                    )
                    loop_start_datetime = _dt.datetime.utcfromtimestamp(loop_start_seconds)
                    signaling_markers = markers_to_signaling(matching_markers)
                    # grave-robber/SCOPE.md §6.2: DATERANGE START-DATE is an
                    # absolute wall-clock value (unlike DASH's Period-relative
                    # <Event presentationTime>, which needs no adjustment --
                    # the Period's own start= already carries the declared
                    # offset, see build_dash_manifest), so each marker's own
                    # declared position must be computed individually here:
                    # real serving-position tick -> whichever segment it
                    # falls in -> that segment's accumulated declared offset.
                    # A no-op (0 for every index) for any package with no
                    # internal asset boundaries.
                    signaling_markers = [
                        dataclasses.replace(
                            sm,
                            pts_time_ticks=sm.pts_time_ticks
                            + pkg.declared_offset_ticks_by_local_index[
                                segment_index_for_position(
                                    sm.pts_time_ticks, pkg.segment_boundary_ticks
                                )
                            ],
                        )
                        for sm in signaling_markers
                    ]
                    if pkg.increment_event_ids:
                        signaling_markers = _remap_signaling_markers(
                            signaling_markers, pkg.markers, local_loop_number
                        )
                    daterange_builder = (
                        build_grouped_daterange_tags
                        if pkg.daterange_mode == "grouped"
                        else build_daterange_tags
                    )
                    lines.extend(
                        daterange_builder(
                            signaling_markers,
                            pkg.timescale,
                            loop_start_datetime,
                            loop_number=local_loop_number,
                            daterange_id_format=pkg.daterange_id_format,
                        )
                    )

            # [markers].cue_tags = "alongside" | "only": #EXT-X-CUE-OUT on
            # the segment where a break opens (or, for a window that opens
            # mid-break, #EXT-X-CUE-OUT-CONT with the correct ELAPSED-TIME
            # right away), #EXT-X-CUE-OUT-CONT on every interior segment,
            # #EXT-X-CUE-IN on the segment where it closes. No raw SCTE-35
            # payload in any of these -- see LoopPackage.cue_breaks.
            if pkg.cue_tags in ("alongside", "only"):
                for brk in pkg.cue_breaks:
                    start, end = brk["start_ticks"], brk["end_ticks"]
                    if ref_seg_start_ticks <= end < ref_seg_end_ticks:
                        # `end` lands exactly on this segment's own start,
                        # by construction (see bake.py: GPAC splits
                        # segments exactly at marker ticks) -- closes the
                        # break regardless of the general overlap test
                        # below, which a boundary-touching interval fails.
                        lines.append(build_cue_in_tag())
                        continue
                    if not (ref_seg_start_ticks < end and start < ref_seg_end_ticks):
                        continue
                    elapsed_ticks = ref_seg_start_ticks - start
                    if brk["event_id"] not in seen_cue_event_ids:
                        seen_cue_event_ids.add(brk["event_id"])
                        if ref_seg_start_ticks <= start < ref_seg_end_ticks:
                            lines.append(build_cue_out_tag(brk["duration_ticks"], pkg.timescale))
                            continue
                    lines.append(
                        build_cue_out_cont_tag(elapsed_ticks, brk["duration_ticks"], pkg.timescale)
                    )

            # grave-robber/SCOPE.md §6.2: the declared position fed into PDT
            # is the real serving-position tick PLUS every gap_ticks crossed
            # by an asset boundary at or before this segment -- the serving
            # position itself (used for the live-edge/window math above) is
            # never perturbed. 0 for every index on a package with no
            # internal asset boundaries, so this is exactly today's value
            # unchanged in that (the common) case.
            declared_segment_start_ticks = (
                segment_start_ticks + pkg.declared_offset_ticks_by_local_index[local_index]
            )
            # Asset-boundary comments (from franken-ts's .timeline.json, see
            # bake.py's load_asset_boundaries): plain `#` playlist comments
            # -- ignored by every HLS client -- naming which playlist asset
            # starts in this segment. Decided from the reference rendition's
            # timeline, same as markers, so every playlist (and the audio
            # playlist) places the same asset's comment at the same segment
            # index. Not deduped across polls (unlike DATERANGE): re-emitting
            # a comment for a segment still in the window on every poll is
            # harmless, exactly like PROGRAM-DATE-TIME itself.
            for asset_id in _asset_ids_starting_in_segment(
                pkg.asset_boundaries, ref_seg_start_ticks, ref_seg_end_ticks
            ):
                lines.append(f"# asset: {asset_id}")

            program_date_ticks = program_date_time_ticks(
                local_loop_number,
                declared_segment_start_ticks,
                pkg.total_loop_duration_ticks,
                self.epoch_ticks,
            )
            program_date_seconds = ticks_to_wall_clock_seconds(
                program_date_ticks, pkg.timescale
            )
            program_date_str = (
                _dt.datetime.utcfromtimestamp(program_date_seconds).strftime(
                    "%Y-%m-%dT%H:%M:%S.%f"
                )[:-3]
                + "Z"
            )

            segment_duration_ticks = seg_end_ticks_local - segment_start_ticks
            segment_duration_seconds = ticks_to_wall_clock_seconds(
                segment_duration_ticks, pkg.timescale
            )

            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{program_date_str}")
            lines.append(f"#EXTINF:{segment_duration_seconds:.3f},")
            # SCOPE.md §12.2: continuity mode's byte-serving routes need the
            # ever-increasing global index (not the plain physical/local
            # one) to know which loop iteration -- and therefore which tick
            # shift -- a given segment request belongs to, since the same
            # physical file is reused every loop. This does trade away the
            # default mode's "one URL forever, cacheable across every loop
            # iteration" property (SCOPE.md §4.2) for continuity mode.
            lines.append(self.segment_uri(seg_uri_template, global_index, local_index, window))

        if window is not None and window.ended:
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    def _iso_ticks(self, ticks: int) -> str:
        seconds = ticks_to_wall_clock_seconds(ticks, self.package.timescale)
        return _dt.datetime.utcfromtimestamp(seconds).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _mpd_open_tag(
        self,
        *,
        window: "TimeWindow | None",
        window_segments: int,
        origin_abs_ticks: int = 0,
        duration_ticks: int = 0,
    ) -> str:
        """The `<MPD ...>` start tag shared by both DASH builders. Live
        (window is None) is exactly the pre-§13 output. A time-shifted range
        (SCOPE.md §13): `availabilityStartTime` is the wall clock of the
        range origin so presentation time 0 == the snapped start; an ended
        range is a static MPD with a fixed duration; a growing one is
        dynamic with the whole range kept (no timeShiftBufferDepth)."""
        pkg = self.package
        max_seg = pkg.max_segment_duration_seconds_rounded_up
        ns = (
            f'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"\n'
            f'     xmlns:scte35="{SCTE35_XML_NAMESPACE}"\n'
            f'     profiles="urn:mpeg:dash:profile:isoff-live:2011"\n'
        )
        if window is not None and window.ended:
            duration = ticks_to_wall_clock_seconds(duration_ticks, pkg.timescale)
            return (
                ns + f'     type="static"\n'
                f'     mediaPresentationDuration="PT{duration}S"\n'
                f'     minBufferTime="PT2S">'
            )
        if window is not None:
            ast = self._iso_ticks(self.epoch_ticks + origin_abs_ticks)
            depth = ""
        else:
            ast = self._iso_ticks(self.epoch_ticks)
            depth = f'     timeShiftBufferDepth="PT{max_seg * window_segments}S"\n'
        return (
            ns + f'     type="dynamic"\n'
            f'     availabilityStartTime="{ast}"\n'
            f'     publishTime="{self._iso_ticks(self.now_ticks())}"\n'
            f'     minimumUpdatePeriod="PT{max_seg}S"\n'
            + depth
            + f'     suggestedPresentationDelay="PT{max_seg * 2}S"\n'
            f'     minBufferTime="PT2S">'
        )

    def build_dash_manifest(
        self, window_segments: int | None = None, window: TimeWindow | None = None
    ) -> str:
        """Build a DASH MPD (@type=dynamic) covering the same sliding window
        of segments as the HLS playlists, using one <AdaptationSet> per
        media type. The video AdaptationSet contains one <Representation>
        per rendition, each with its OWN <SegmentTemplate>/<SegmentTimeline>
        (DASH allows this at Representation level) since each rendition's
        segment boundary tick VALUES were snapped independently (same
        reasoning as audio -- see LoopPackage/VideoRendition docstrings).
        The <EventStream> carries the same markers as HLS's DATERANGE tags,
        authored directly from markers.json (never GPAC's own aggregation
        -- SCOPE.md §6).

        One <Period> per loop iteration covered by the current window,
        each starting at `PT{loop_number * total_loop_duration_seconds}S`
        (real wall-clock offset from availabilityStartTime). This mirrors
        the HLS side's #EXT-X-DISCONTINUITY fix for the same underlying
        reason: every physical segment file is reused byte-for-byte on
        every loop iteration (SCOPE.md §4.1 step 6), so its internal fMP4
        timestamps (tfdt/baseMediaDecodeTime) always restart from the same
        loop-relative values regardless of which real loop iteration is
        being served. A single, never-restarted Period whose
        <SegmentTimeline> `t` values grow forever across loops would assert
        an ever-increasing presentation timeline while the underlying
        media's actual decode timestamps repeatedly reset -- exactly the
        HLS "no discontinuity signaled" bug, just via DASH's own
        discontinuity mechanism (Periods) instead of HLS's
        #EXT-X-DISCONTINUITY tag. Starting a new Period at each loop
        boundary makes every Period's own <SegmentTimeline> genuinely
        period-relative (starting back at the segment's own loop-relative
        tick), consistent with what's actually inside the segment files,
        and gives players an explicit, spec-compliant boundary to reset
        timestamp-continuity expectations at -- rather than a single Period
        silently lying about being one continuous timeline forever.

        Within the currently OPEN (still loop-in-progress) Period, the
        <SegmentTimeline> is never front-pruned/renumbered across requests
        -- see the `open_count`/`periods_plan` comment below for why that's
        both required (real DASH clients, e.g. dash.js, get stuck forever
        once a still-growing Period's timeline is trimmed out from under
        them) and safe to do without violating serve.py's "no persisted
        state" rule (bounded naturally by `segments_per_loop`).
        """
        if self.continuous:
            return self._build_dash_manifest_continuous(window_segments, window)

        window_segments = window_segments or self.window_segments
        pkg = self.package
        if window is not None:
            seg_index = window.last_global % pkg.segments_per_loop
            current_loop_number = window.last_global // pkg.segments_per_loop
        else:
            pos = self.current_position()
            seg_index = segment_index_for_position(
                pos.position_in_loop_ticks, pkg.segment_boundary_ticks
            )
            current_loop_number = pos.loop_number


        # Build the list of (loop_number, [local_index, ...]) pairs, one per
        # <Period> to emit. Crucially, the CURRENTLY OPEN loop iteration
        # (current_loop_number) always gets every one of its segments from
        # local index 0 up to the current live edge (seg_index) -- it is
        # NEVER front-pruned to fit `window_segments`.
        #
        # This is a real DASH client requirement, not a style choice: once a
        # player has parsed a Period's <SegmentTimeline> and started walking
        # forward through it, it expects each subsequent MPD refresh for
        # that SAME (still-open) Period to be a strict superset of what it
        # already saw -- new <S> entries appended at the tail, never a
        # front-trimmed/renumbered replacement. A naive fixed-size sliding
        # window applied to the currently-growing Period breaks that
        # invariant every time the window slides, which was observed to
        # make dash.js's reference player get stuck forever repeating
        # "No segment found at index: N. Wait for next loop" once the
        # window had slid a few segments past where it started watching.
        #
        # This is safe/bounded without any persisted state (SCOPE.md's
        # "no accumulated state" rule): local index 0..seg_index is a
        # small, fully deterministic function of current wall-clock time,
        # capped at `segments_per_loop` entries (one whole loop) -- it can
        # never grow unboundedly, since a new Period starts at the next
        # loop wrap regardless.
        #
        # Only PAST, already-closed loop iterations (loop_number <
        # current_loop_number) are eligible for window-based trimming --
        # their own <SegmentTimeline> is permanently fixed/immutable content
        # (that loop iteration already fully happened and will never gain
        # new segments), so repeatedly re-serving the same fixed tail slice
        # of a closed Period across polls is fully consistent and safe.
        # Used only to pad out extra DVR history when the current loop
        # hasn't yet produced `window_segments` worth of its own segments
        # (e.g. right after a loop wrap).
        # grave-robber/SCOPE.md §6.1: generalizes "one Period per loop
        # iteration" to "one Period per asset span" -- a span being the run
        # of segments between consecutive entries of pkg.boundaries (always
        # includes local index 0, the loop wrap). With no internal asset
        # boundaries (pkg.boundaries == {0}), there is exactly one span per
        # loop and every span_bounds/span_index_for_local call below reduces
        # to the original whole-loop behavior.
        boundaries_sorted = sorted(pkg.boundaries)
        spans_per_loop = len(boundaries_sorted)

        def _span_bounds(span_index: int) -> tuple[int, int]:
            """[start_local, end_local) for asset span `span_index`."""
            start = boundaries_sorted[span_index]
            end = (
                boundaries_sorted[span_index + 1]
                if span_index + 1 < spans_per_loop
                else pkg.segments_per_loop
            )
            return start, end

        def _span_index_for_local(local_index: int) -> int:
            idx = 0
            for i, boundary in enumerate(boundaries_sorted):
                if boundary <= local_index:
                    idx = i
                else:
                    break
            return idx

        periods_plan: list[tuple[int, int, list[int]]] = []
        if window is not None:
            # SCOPE.md §13: one Period per (loop, span) run covering the
            # whole [first_global, last_global] range -- never front-pruned.
            for g in range(window.first_global, window.last_global + 1):
                g_loop, g_local = divmod(g, pkg.segments_per_loop)
                g_span = _span_index_for_local(g_local)
                if periods_plan and periods_plan[-1][0] == g_loop and periods_plan[-1][1] == g_span:
                    periods_plan[-1][2].append(g_local)
                else:
                    periods_plan.append((g_loop, g_span, [g_local]))
        else:
            current_span_index = _span_index_for_local(seg_index)
            current_span_start, _ = _span_bounds(current_span_index)
            # Segments served so far in the CURRENT open span -- never
            # front-pruned (same DASH-client requirement as before, now scoped
            # to the span rather than the whole loop).
            open_count = seg_index - current_span_start + 1

            if open_count < window_segments:
                if current_span_index > 0:
                    prev_loop_number, prev_span_index = current_loop_number, current_span_index - 1
                elif current_loop_number > 0:
                    prev_loop_number, prev_span_index = current_loop_number - 1, spans_per_loop - 1
                else:
                    prev_loop_number = None
                if prev_loop_number is not None:
                    prev_start, prev_end = _span_bounds(prev_span_index)
                    needed_from_prev = min(window_segments - open_count, prev_end - prev_start)
                    start_local = prev_end - needed_from_prev
                    periods_plan.append(
                        (prev_loop_number, prev_span_index, list(range(start_local, prev_end)))
                    )
            periods_plan.append(
                (current_loop_number, current_span_index, list(range(current_span_start, seg_index + 1)))
            )


        def _period_entries(
            boundary_ticks: list[int], local_indices: list[int], span_start_local: int
        ) -> list[tuple[int, int, int]]:
            """(segment_start_ticks, duration_ticks, local_index), relative
            to this Period's own start (span_start_local's own tick), not
            the whole loop's -- identical to loop-relative when
            span_start_local == 0 (the common, no-internal-boundary case)."""
            span_start_ticks = boundary_ticks[span_start_local]
            entries = []
            for local_index in local_indices:
                segment_start_ticks = boundary_ticks[local_index]
                if local_index + 1 < len(boundary_ticks):
                    seg_end_ticks_local = boundary_ticks[local_index + 1]
                else:
                    seg_end_ticks_local = pkg.total_loop_duration_ticks
                duration_ticks = seg_end_ticks_local - segment_start_ticks
                entries.append((segment_start_ticks - span_start_ticks, duration_ticks, local_index))
            return entries

        window_origin_abs = 0
        window_duration_ticks = 0
        if window is not None:
            first_loop, _first_span, first_locals = periods_plan[0]
            window_origin_abs = (
                first_loop * pkg.total_loop_duration_ticks
                + pkg.segment_boundary_ticks[first_locals[0]]
                + pkg.declared_offset_ticks_by_local_index[first_locals[0]]
            )
            last_loop, _last_span, last_locals = periods_plan[-1]
            last_local = last_locals[-1]
            last_end_local = (
                pkg.segment_boundary_ticks[last_local + 1]
                if last_local + 1 < len(pkg.segment_boundary_ticks)
                else pkg.total_loop_duration_ticks
            )
            window_duration_ticks = (
                last_loop * pkg.total_loop_duration_ticks
                + last_end_local
                + pkg.declared_offset_ticks_by_local_index[last_local]
                - window_origin_abs
            )

        period_xml_parts = []
        for period_number, (loop_number, span_index, local_indices) in enumerate(periods_plan):
            if not local_indices:
                continue
            span_start_local, _ = _span_bounds(span_index)
            # grave-robber/SCOPE.md §6.2: declared position (real serving
            # position + every gap_ticks crossed so far) drives the
            # Period's own start=, exactly mirroring the HLS side's PDT
            # computation -- 0 offset (i.e. today's plain
            # loop_number*total_loop_duration value) whenever there are no
            # internal asset boundaries.
            period_start_ticks_relative = (
                loop_number * pkg.total_loop_duration_ticks
                + pkg.segment_boundary_ticks[span_start_local]
                + pkg.declared_offset_ticks_by_local_index[span_start_local]
            )
            # SCOPE.md §13: rebase a time-shifted range so its first segment is
            # presentation time 0 -- the first Period starts at 0 with a
            # presentationTimeOffset covering the lead-in to its first
            # segment; later Periods start relative to the range origin.
            pto_ticks = 0
            if window is not None:
                if period_number == 0:
                    pto_ticks = window_origin_abs - period_start_ticks_relative
                    period_start_ticks_relative = 0
                else:
                    period_start_ticks_relative -= window_origin_abs
            pto_attr = f' presentationTimeOffset="{pto_ticks}"' if pto_ticks else ""
            period_start_seconds = ticks_to_wall_clock_seconds(
                period_start_ticks_relative, pkg.timescale
            )
            first_number = loop_number * pkg.segments_per_loop + local_indices[0]

            # Reference (ad-decision authority) entries for this period,
            # used for marker placement -- loop-relative ticks, since each
            # Period's own <EventStream> is independently time-based from
            # its own start.
            reference_entries = _period_entries(
                pkg.segment_boundary_ticks, local_indices, span_start_local
            )

            # Asset-boundary comments (from franken-ts's .timeline.json, see
            # bake.py's load_asset_boundaries): decided once from the
            # reference rendition's timeline, keyed by local_index so every
            # rendition's own <SegmentTimeline> (and audio's) places the
            # same asset's comment at the same segment index -- same
            # pattern as marker placement above.
            asset_ids_by_local_index: dict[int, list[str]] = {}
            span_offset_ticks = pkg.segment_boundary_ticks[span_start_local]
            for seg_start_local, duration_ticks, local_index in reference_entries:
                # reference_entries are Period-relative; asset boundaries are loop-relative.
                asset_ids = _asset_ids_starting_in_segment(
                    pkg.asset_boundaries,
                    seg_start_local + span_offset_ticks,
                    seg_start_local + span_offset_ticks + duration_ticks,
                )
                if asset_ids:
                    asset_ids_by_local_index[local_index] = asset_ids

            def _segment_timeline_xml(entries: list[tuple[int, int, int]]) -> str:
                lines = []
                for t, d, local_index in entries:
                    for asset_id in asset_ids_by_local_index.get(local_index, []):
                        lines.append(f'        <!-- asset: {asset_id} -->')
                    lines.append(f'        <S t="{t}" d="{d}" />')
                return "\n".join(lines)

            # `id` is a per-Period-unique synthetic id, never the raw
            # event_id directly -- see DIRECTION_CODE below. A player
            # decodes the Signal payload itself (via the `scte35` npm
            # package, same as HLS -- see igor's scte35Lite.ts
            # describeAllMarkers()) to get the real segmentation/splice
            # event id and type, rather than reading `id`.
            #
            # A Start/End pair SHARES one event_id by design (see the
            # module docstring on event_id reuse), and once enough of a
            # loop iteration has played out that BOTH halves sit inside the
            # currently-open Period's never-pruned segment range (see the
            # comment above `periods_plan`), they're both due in the same
            # single <EventStream> -- if `id` were the raw event_id,
            # dash.js's EventController (confirmed against its own source)
            # would see the second one as a duplicate of the first (same
            # id) and silently drop it, so the End marker would never
            # fire. `DIRECTION_CODE` gives each direction (out/in/instant)
            # its own multiple of the base event id, so the two halves of
            # one break -- and the exact same marker recurring next loop --
            # always get distinct, but still fully deterministic (no
            # runtime counter), ids. Base event ids must stay well under
            # 2**30 for this to fit `id`'s xs:unsignedInt range once
            # multiplied -- true for every id this codebase's channel
            # configs actually assign.
            # [markers].increment_event_ids: one id map per Period, keyed
            # by that Period's own loop_number (same helper HLS uses, see
            # _remap_signaling_markers) -- {} when the setting is off, so
            # the lookups below become no-ops via dict.get() fallback.
            event_id_map = build_event_id_map(pkg.markers, loop_number) if pkg.increment_event_ids else {}
            DIRECTION_CODE = {"out": 0, "in": 1, "instant": 2}

            # [markers].dash_signal_format: "binary" (default) carries the
            # raw base64 splice command in a <scte35:Binary> element; "xml"
            # carries each marker's full decoded <scte35:SpliceInfoSection>
            # instead (see build_scte35_full_xml). schemeIdUri distinguishes
            # the two per the SCTE-35 XML binding, constant for the whole
            # manifest.
            scheme_id_uri = (
                SCTE35_XML_NAMESPACE
                if pkg.dash_signal_format == "xml"
                else "urn:scte:scte35:2014:xml+bin"
            )

            event_xml: list[str] = []
            for marker in pkg.markers:
                # A single <Event> element describes the whole
                # [presentationTime, presentationTime+duration) interval on
                # its own -- unlike HLS's per-segment EXT-X-DATERANGE tags,
                # it doesn't need repeating once per overlapping segment.
                # But it must still be included in this Period as long as
                # ANY of the Period's currently-served segments overlaps
                # it (see _marker_covers_segment) -- not just the one
                # segment containing the marker's own start tick -- so a
                # player whose manifest poll lands after that first segment
                # has scrolled out of the window (while later segments of
                # the same break are still being served) still sees it.
                if not any(
                    _marker_covers_segment(marker, seg_start_local, seg_start_local + duration_ticks)
                    for seg_start_local, duration_ticks, _local_index in reference_entries
                ):
                    continue
                duration_attr = (
                    f' duration="{marker["segmentation_duration_ticks"]}"'
                    if marker.get("segmentation_duration_ticks") is not None
                    else ""
                )
                event_id_hex = event_id_map.get(marker["event_id"], marker["event_id"])
                event_id_dec = int(event_id_hex, 16)
                # [markers].dash_descriptor_mode: "shared" (default) embeds
                # the same full multi-descriptor message every coincident
                # marker carries; "narrowed" embeds the per-event,
                # single-descriptor re-encode instead -- independent of
                # whatever daterange_mode HLS is using (see bake.py's
                # DecodedMarker.splice_command_b64_narrowed).
                base_b64 = (
                    marker["splice_command_b64_narrowed"]
                    if pkg.dash_descriptor_mode == "narrowed"
                    else marker["splice_command_b64"]
                )
                splice_command_b64 = (
                    reencode_event_ids(base_b64, event_id_map)
                    if pkg.increment_event_ids
                    else base_b64
                )
                if is_instant_segmentation(marker):
                    direction = "instant"
                elif is_out_marker(marker):
                    direction = "out"
                else:
                    direction = "in"
                synthetic_id = event_id_dec * 4 + DIRECTION_CODE[direction]
                if pkg.dash_signal_format == "xml":
                    full_xml = build_scte35_full_xml(splice_command_b64)
                    indented_xml = "\n".join(
                        f"        {line}" for line in full_xml.splitlines()
                    )
                    signal_xml = (
                        f"      <scte35:Signal>\n"
                        f"{indented_xml}\n"
                        f"      </scte35:Signal>"
                    )
                else:
                    signal_xml = (
                        f"      <scte35:Signal>\n"
                        f"        <scte35:Binary>{splice_command_b64}</scte35:Binary>\n"
                        f"      </scte35:Signal>"
                    )
                # Period-relative, like <S t=...> -- subtract this Period's
                # own start tick (grave-robber/SCOPE.md §6.2's declared
                # offset lives in the Period's start= instead, see above; a
                # no-op subtraction of pkg.segment_boundary_ticks[0]==0 in
                # the common single-span-per-loop case).
                event_presentation_time = (
                    marker["pts_time_ticks"] - pkg.segment_boundary_ticks[span_start_local] - pto_ticks
                )
                event_xml.append(
                    f'    <Event presentationTime="{event_presentation_time}"'
                    f'{duration_attr} id="{synthetic_id}">\n'
                    f"{signal_xml}\n"
                    f"    </Event>"
                )

            video_representations = []
            for idx, rendition in enumerate(pkg.video_renditions):
                entries = _period_entries(
                    rendition.segment_boundary_ticks, local_indices, span_start_local
                )
                timeline_lines = _segment_timeline_xml(entries)
                v = rendition.video_variant
                # A sparse (self-initializing) rendition's segments carry
                # their own moov (SCOPE.md §11) -- there's no shared init
                # segment to point `initialization=` at, same reasoning as
                # HLS's #EXT-X-MAP omission above.
                if rendition.self_initializing:
                    init_attr = ""
                elif rendition.shared_init:
                    init_attr = f' initialization="{rendition.name}/init_{span_index}.mp4"'
                else:
                    init_attr = f' initialization="{rendition.name}/init.mp4"'

                video_representations.append(f'''      <Representation id="v{idx}" bandwidth="{v["bandwidth"]}" codecs="{v["codecs"]}" width="{v["width"]}" height="{v["height"]}" frameRate="{v["frame_rate"]:.3f}">
        <SegmentTemplate media="{rendition.name}/seg/$Number$.m4s"{init_attr}
                         timescale="{pkg.timescale}" startNumber="{first_number}"{pto_attr}>
          <SegmentTimeline>
{timeline_lines}
          </SegmentTimeline>
        </SegmentTemplate>
      </Representation>''')

            audio_adaptation_set = ""
            if pkg.has_audio:
                a = pkg.audio_rendition.audio_variant
                audio_init = (
                    f"audio/init_{span_index}.mp4" if pkg.audio_rendition.audio_sparse else "audio/init.mp4"
                )
                audio_entries = _period_entries(
                    pkg.audio_rendition.audio_segment_boundary_ticks, local_indices, span_start_local
                )
                audio_timeline_lines = _segment_timeline_xml(audio_entries)
                audio_adaptation_set = f'''
    <AdaptationSet mimeType="audio/mp4" segmentAlignment="true" startWithSAP="1">
      <Representation id="a0" bandwidth="{a["bandwidth"]}" codecs="{a["codecs"]}">
        <SegmentTemplate media="audio/seg/$Number$.m4s" initialization="{audio_init}"
                         timescale="{pkg.timescale}" startNumber="{first_number}"{pto_attr}>
          <SegmentTimeline>
{audio_timeline_lines}
          </SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>'''

            # With more than one Period per loop (grave-robber/SCOPE.md §6.1), give each
            # Period's EventStream its own @value so dash.js's (value, id) dedupe key
            # can't fold events from two Periods of the same loop together.
            stream_value_attr = f' value="{loop_number}-{span_index}"' if spans_per_loop > 1 else ""
            event_streams_xml = (
                f'    <EventStream schemeIdUri="{scheme_id_uri}" timescale="{pkg.timescale}"{stream_value_attr}>\n'
                + "\n".join(event_xml)
                + "\n    </EventStream>"
                if event_xml else ""
            )

            period_id = (
                f"loop{loop_number}"
                if spans_per_loop == 1
                else f"loop{loop_number}-{span_start_local}"
            )
            period_xml_parts.append(f'''  <Period id="{period_id}" start="PT{period_start_seconds}S">
{event_streams_xml}
    <AdaptationSet mimeType="video/mp4" segmentAlignment="true" startWithSAP="1">
{chr(10).join(video_representations)}
    </AdaptationSet>{audio_adaptation_set}
  </Period>''')

        # @suggestedPresentationDelay tells compliant players to deliberately
        # stay this far behind the true live edge, rather than chasing it as
        # closely as possible. Without it, aggressive live-catchup players
        # (e.g. dash.js's default CatchupController) can outrun the actual
        # availability of the newest segment -- there is always up to one
        # segment-duration's worth of "not yet aired" content sitting right
        # at the live edge for any live stream (looping or not), since a
        # segment can't be advertised before its content has actually
        # occurred in wall-clock time. Observed effect without this: players
        # occasionally ran into that always-there edge gap and invoked their
        # own "jump the gap" recovery instead of just comfortably waiting
        # behind it. Two segment durations of delay margin is a
        # conservative, standard buffer for this.
        mpd_open = self._mpd_open_tag(
            window=window,
            window_segments=window_segments,
            origin_abs_ticks=window_origin_abs,
            duration_ticks=window_duration_ticks,
        )
        mpd = f'''<?xml version="1.0" encoding="utf-8"?>
{mpd_open}
{chr(10).join(period_xml_parts)}
</MPD>
'''
        return mpd

    def _build_dash_manifest_continuous(
        self, window_segments: int | None = None, window: TimeWindow | None = None
    ) -> str:
        """SCOPE.md §12.3: continuity mode's DASH output is ONE Period that
        never restarts -- its <SegmentTimeline> `t` values are each
        segment's real ABSOLUTE tick position (`loop_number *
        total_loop_duration_ticks` + its loop-relative tick), continuously
        increasing across every loop wrap, matching what the byte-serving
        routes actually rewrite the segments' own `tfdt` to. This is the
        DASH-side equivalent of the default mode's per-loop <Period> restart
        (see build_dash_manifest's own docstring) -- continuity mode's whole
        point is that there is no discontinuity left to give a Period
        boundary to.

        Only reached for a package that already passed
        `Channel._validate_continuous` (boundaries == {0}) -- so, unlike
        build_dash_manifest, there is exactly one span per loop. A sparse
        rendition (SCOPE.md §11) is allowed through as long as it has no
        internal joins, so `init_attr`/`audio_init` below still need to
        pick the right init-segment shape (self-initializing / per-span /
        shared) -- span index is always 0 when there's only one span.
        """
        window_segments = window_segments or self.window_segments
        pkg = self.package
        if window is not None:
            # SCOPE.md §13: explicit range; presentation time 0 is the first
            # segment (presentationTimeOffset below), t values stay absolute
            # so they match the tfdt the cseg route writes.
            first_global_index, media_sequence = window.first_global, window.last_global
        else:
            pos = self.current_position()
            seg_index = segment_index_for_position(
                pos.position_in_loop_ticks, pkg.segment_boundary_ticks
            )
            media_sequence = global_segment_number(pos.loop_number, seg_index, pkg.segments_per_loop)
            first_global_index = max(0, media_sequence - window_segments + 1)
        global_indices = list(range(first_global_index, media_sequence + 1))
        first_loop, first_local = divmod(first_global_index, pkg.segments_per_loop)
        last_loop, last_local = divmod(media_sequence, pkg.segments_per_loop)
        origin_abs = (
            first_loop * pkg.total_loop_duration_ticks + pkg.segment_boundary_ticks[first_local]
            if window is not None
            else 0
        )
        end_abs = last_loop * pkg.total_loop_duration_ticks + (
            pkg.segment_boundary_ticks[last_local + 1]
            if last_local + 1 < len(pkg.segment_boundary_ticks)
            else pkg.total_loop_duration_ticks
        )
        pto_attr = f' presentationTimeOffset="{origin_abs}"' if origin_abs else ""


        def _entries(boundary_ticks: list[int]) -> list[tuple[int, int, int]]:
            """(absolute_start_ticks, duration_ticks, local_index) -- this
            Period's own start is fixed at tick 0 forever, so "absolute" and
            "Period-relative" are the same thing here."""
            entries = []
            for global_index in global_indices:
                loop_number, local_index = divmod(global_index, pkg.segments_per_loop)
                start = boundary_ticks[local_index]
                end = (
                    boundary_ticks[local_index + 1]
                    if local_index + 1 < len(boundary_ticks)
                    # The first segment need not start at tick zero (AAC
                    # typically starts a frame later than video). Its next
                    # iteration starts at loop_duration + boundary_ticks[0],
                    # not at loop_duration. Otherwise the MPD describes an
                    # audio gap at every wrap even though the shifted tfdt
                    # and media samples continue through it. dash.js can
                    # stop requesting audio at that gap and stall playback.
                    else pkg.total_loop_duration_ticks + boundary_ticks[0]
                )
                entries.append(
                    (loop_number * pkg.total_loop_duration_ticks + start, end - start, local_index)
                )
            return entries

        def _segment_timeline_xml(entries: list[tuple[int, int, int]]) -> str:
            return "\n".join(f'        <S t="{t}" d="{d}" />' for t, d, _local_index in entries)

        # Markers: one <Event> per (event_id, loop_number) occurrence whose
        # interval overlaps the window -- mirrors the per-Period builder's
        # own once-per-marker-per-Period dedup, just across the whole window
        # rather than per Period, since there's only ever one Period now.
        DIRECTION_CODE = {"out": 0, "in": 1, "instant": 2}
        scheme_id_uri = (
            SCTE35_XML_NAMESPACE if pkg.dash_signal_format == "xml" else "urn:scte:scte35:2014:xml+bin"
        )
        event_xml: list[str] = []
        emitted: set[tuple[str, int]] = set()
        for global_index in global_indices:
            loop_number, local_index = divmod(global_index, pkg.segments_per_loop)
            ref_start = pkg.segment_boundary_ticks[local_index]
            ref_end = (
                pkg.segment_boundary_ticks[local_index + 1]
                if local_index + 1 < len(pkg.segment_boundary_ticks)
                else pkg.total_loop_duration_ticks
            )
            event_id_map = build_event_id_map(pkg.markers, loop_number) if pkg.increment_event_ids else {}
            for marker in pkg.markers:
                key = (marker["event_id"], loop_number)
                if key in emitted or not _marker_covers_segment(marker, ref_start, ref_end):
                    continue
                emitted.add(key)
                duration_attr = (
                    f' duration="{marker["segmentation_duration_ticks"]}"'
                    if marker.get("segmentation_duration_ticks") is not None
                    else ""
                )
                event_id_hex = event_id_map.get(marker["event_id"], marker["event_id"])
                event_id_dec = int(event_id_hex, 16)
                base_b64 = (
                    marker["splice_command_b64_narrowed"]
                    if pkg.dash_descriptor_mode == "narrowed"
                    else marker["splice_command_b64"]
                )
                splice_command_b64 = (
                    reencode_event_ids(base_b64, event_id_map) if pkg.increment_event_ids else base_b64
                )
                if is_instant_segmentation(marker):
                    direction = "instant"
                elif is_out_marker(marker):
                    direction = "out"
                else:
                    direction = "in"
                if pkg.dash_signal_format == "xml":
                    full_xml = build_scte35_full_xml(splice_command_b64)
                    indented_xml = "\n".join(f"        {line}" for line in full_xml.splitlines())
                    signal_xml = f"      <scte35:Signal>\n{indented_xml}\n      </scte35:Signal>"
                else:
                    signal_xml = (
                        f"      <scte35:Signal>\n"
                        f"        <scte35:Binary>{splice_command_b64}</scte35:Binary>\n"
                        f"      </scte35:Signal>"
                    )
                # A recurring marker gets a distinct id per loop_number it's
                # re-emitted at (a single ever-open Period, unlike the
                # default mode's one-namespace-per-Period, needs this to
                # avoid dash.js's EventController silently dropping a
                # same-id "duplicate" -- see build_dash_manifest's own
                # DIRECTION_CODE comment for the base id math). Only the
                # low bit of loop_number is folded in: a DVR window
                # (SCOPE.md §12) spanning more than two loop iterations at
                # once would need more room than this, but that is already
                # a pathological window/loop-duration combination, not
                # something continuity mode introduces.
                base_id = event_id_dec * 4 + DIRECTION_CODE[direction]
                if window is None:
                    synthetic_id = base_id * 2 + (loop_number & 1)
                else:
                    # A range can span many loops and must keep stable ids
                    # as a growing manifest is refreshed: fold in the low 16
                    # bits of the absolute loop number (SCOPE.md §13.6).
                    synthetic_id = (base_id << 16) | (loop_number & 0xFFFF)
                event_presentation_time = (
                    loop_number * pkg.total_loop_duration_ticks + marker["pts_time_ticks"] - origin_abs
                )
                event_xml.append(
                    f'    <Event presentationTime="{event_presentation_time}"'
                    f'{duration_attr} id="{synthetic_id}">\n'
                    f"{signal_xml}\n"
                    f"    </Event>"
                )

        event_streams_xml = (
            f'    <EventStream schemeIdUri="{scheme_id_uri}" timescale="{pkg.timescale}">\n'
            + "\n".join(event_xml)
            + "\n    </EventStream>"
            if event_xml else ""
        )

        video_representations = []
        for idx, rendition in enumerate(pkg.video_renditions):
            timeline_lines = _segment_timeline_xml(_entries(rendition.segment_boundary_ticks))
            v = rendition.video_variant
            # SCOPE.md §12.6: a sparse rendition may be self-initializing
            # (own moov, no shared init at all) or carry a per-span init
            # (span always 0 here, since boundaries == {0} means one span)
            # -- same three-way choice build_dash_manifest's own
            # per-Period builder makes, just with span_index fixed at 0.
            if rendition.self_initializing:
                init_attr = ""
            elif rendition.shared_init:
                init_attr = f' initialization="{rendition.name}/init_0.mp4"'
            else:
                init_attr = f' initialization="{rendition.name}/init.mp4"'
            video_representations.append(f'''      <Representation id="v{idx}" bandwidth="{v["bandwidth"]}" codecs="{v["codecs"]}" width="{v["width"]}" height="{v["height"]}" frameRate="{v["frame_rate"]:.3f}">
        <SegmentTemplate media="{rendition.name}/cseg/$Number$.m4s"{init_attr}
                         timescale="{pkg.timescale}" startNumber="{first_global_index}"{pto_attr}>
          <SegmentTimeline>
{timeline_lines}
          </SegmentTimeline>
        </SegmentTemplate>
      </Representation>''')

        audio_adaptation_set = ""
        if pkg.has_audio:
            a = pkg.audio_rendition.audio_variant
            audio_timeline_lines = _segment_timeline_xml(
                _entries(pkg.audio_rendition.audio_segment_boundary_ticks)
            )
            # Same span-0 reasoning as video's init_attr above -- audio has
            # no self-initializing variant of its own (only sparse/init_N
            # vs. shared, see LoopPackage/VideoRendition).
            audio_init = (
                "audio/init_0.mp4" if pkg.audio_rendition.audio_sparse else "audio/init.mp4"
            )
            audio_adaptation_set = f'''
    <AdaptationSet mimeType="audio/mp4" segmentAlignment="true" startWithSAP="1">
      <Representation id="a0" bandwidth="{a["bandwidth"]}" codecs="{a["codecs"]}">
        <SegmentTemplate media="audio/cseg/$Number$.m4s" initialization="{audio_init}"
                         timescale="{pkg.timescale}" startNumber="{first_global_index}"{pto_attr}>
          <SegmentTimeline>
{audio_timeline_lines}
          </SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>'''

        period_xml = f'''  <Period id="continuous" start="PT0S">
{event_streams_xml}
    <AdaptationSet mimeType="video/mp4" segmentAlignment="true" startWithSAP="1">
{chr(10).join(video_representations)}
    </AdaptationSet>{audio_adaptation_set}
  </Period>'''

        mpd_open = self._mpd_open_tag(
            window=window,
            window_segments=window_segments,
            origin_abs_ticks=origin_abs,
            duration_ticks=end_abs - origin_abs,
        )
        mpd = f'''<?xml version="1.0" encoding="utf-8"?>
{mpd_open}
{period_xml}
</MPD>
'''
        return mpd

    def segment_bytes_path(self, rendition_name: str, physical_index: int) -> Path:
        return self.package.rendition_by_name(rendition_name).segment_path_for_index(physical_index)

    def audio_segment_bytes_path(self, physical_index: int) -> Path:
        assert self.package.audio_rendition is not None
        return self.package.audio_rendition.audio_segment_path_for_index(physical_index)

    def hls_ts_segment_path(self, rendition_name: str, physical_index: int) -> Path | None:
        """Returns None for a sparse-mode index with no physical TS file
        (SCOPE.md §11.3's 404 guard, applied to the hls_format='ts' store
        as well as the CMAF one)."""
        rendition = self.package.rendition_by_name(rendition_name)
        if physical_index >= self.package.segments_per_loop:
            raise IndexError(physical_index)
        path = self.package.package_dir / "hls-ts" / rendition.name / f"{physical_index}.ts"
        if rendition.sparse and not path.exists():
            return None
        return path


def create_app(
    package_dir: Path,
    epoch_ticks: int,
    window_segments: int = 6,
    continuous: bool = False,
    timeshift: TimeshiftConfig | None = None,
) -> Flask:
    ts_cfg = timeshift or TimeshiftConfig()
    package = LoopPackage(package_dir)
    channel = Channel(package, epoch_ticks, window_segments=window_segments, continuous=continuous)
    process_start_ticks = channel.now_ticks()

    app = Flask(__name__)

    def _serve_segment(
        path: Path | None,
        mimetype: str,
        *,
        ts: bool,
        shift_ticks: int | None,
        sequence_number: int = 0,
    ):
        """Shared byte-serving tail for every segment route (SCOPE.md §12):
        the untouched static-file response in default mode, or a per-
        request continuity.py patch (never re-muxed, just the fixed-width
        timestamp fields rewritten) when `shift_ticks` is not None."""
        if path is None:
            abort(404)
        if shift_ticks is None:
            return send_file(path, mimetype=mimetype)
        data = path.read_bytes()
        patched = (
            continuity.shift_ts_segment(data, shift_ticks)
            if ts
            else continuity.shift_cmaf_fragment(data, shift_ticks, sequence_number=sequence_number)
        )
        return Response(patched, mimetype=mimetype)

    @app.after_request
    def _add_cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    def _request_view() -> tuple[Channel, TimeWindow | None, str]:
        """SCOPE.md §13: resolve this request's (channel variant, time-shift
        window, query string to propagate to child playlists). Without
        timeshift enabled -- or without the params -- this is (channel,
        None, "") and behavior is exactly the pre-§13 live stream."""
        ch, window = channel, None
        if ts_cfg.enabled:
            args = request.args
            try:
                raw_cont = args.get(ts_cfg.continuous_param)
                if raw_cont is not None:
                    ch = channel.for_mode(parse_bool(raw_cont, ts_cfg.continuous_param))
                raw_start, raw_end = args.get(ts_cfg.start_param), args.get(ts_cfg.end_param)
                if raw_start is None and raw_end is not None:
                    raise TimeshiftError(f"'{ts_cfg.end_param}' requires '{ts_cfg.start_param}'")
                if raw_start is not None:
                    ts = package.timescale
                    window = ch.resolve_timeshift(
                        parse_instant_ticks(raw_start, ts, ts_cfg.start_param),
                        parse_instant_ticks(raw_end, ts, ts_cfg.end_param) if raw_end is not None else None,
                        ts_cfg.max_span_seconds,
                        parse_bool(args.get(ts_cfg.full_loop_param, "false"), ts_cfg.full_loop_param),
                    )
            except TimeshiftError as exc:
                abort(Response(f"{exc}\n", status=400, mimetype="text/plain"))
            query = urlencode([(k, args[k]) for k in ts_cfg.param_names if k in args])
        else:
            query = ""
        return ch, window, query

    def _manifest_response(body: str, mimetype: str, window: TimeWindow | None) -> Response:
        resp = Response(body, mimetype=mimetype)
        if window is not None:
            # An ended range never changes again; a growing one changes once
            # per segment. Live responses keep whatever caching they had.
            resp.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
                if window.ended
                else f"public, max-age={package.max_segment_duration_seconds_rounded_up}"
            )
        return resp

    def _decode_segment_index(index: int, mode: str, origin_loop: int | None) -> tuple[int, int | None]:
        """(local_index, shift_ticks) for a segment URL (SCOPE.md §13.5):
        `seg` -> index is the local one, bytes untouched; `cseg` -> index is
        global, shift by loop*D; `rseg` -> global, shift relative to
        `origin_loop`."""
        if mode == "local":
            return index, None
        try:
            ch = channel.for_mode(True)
        except TimeshiftError:
            abort(404)
        loop_number, local_index = ch.loop_number_and_local_index(index)
        if mode == "origin":
            if origin_loop is None or loop_number < origin_loop:
                abort(404)
            return local_index, ch.continuity_shift_ticks(loop_number - origin_loop)
        return local_index, ch.continuity_shift_ticks(loop_number)

    @app.get("/health")
    def health():
        """Lightweight JSON status for external monitoring (e.g. a channel
        management UI). Not part of the HLS/DASH viewer-facing contract --
        safe to poll frequently, cheap (no file I/O beyond what's already
        loaded in memory at startup)."""
        pos = channel.current_position()
        now_ticks = channel.now_ticks()
        return {
            "status": "ok",
            "package_dir": str(package_dir),
            "timescale": package.timescale,
            "epoch_ticks": epoch_ticks,
            "total_loop_duration_ticks": package.total_loop_duration_ticks,
            "total_loop_duration_seconds": package.total_loop_duration_ticks / package.timescale,
            "loop_number": pos.loop_number,
            "position_in_loop_ticks": pos.position_in_loop_ticks,
            "position_in_loop_seconds": pos.position_in_loop_ticks / package.timescale,
            "uptime_seconds": (now_ticks - process_start_ticks) / package.timescale,
            "renditions": [r.name for r in package.video_renditions],
            "has_audio": package.has_audio,
            "hls_format": package.hls_format,
            "hls_ts_mux_audio": package.hls_ts_mux_audio,
            "window_segments": channel.window_segments,
            "continuous_timeline": channel.continuous,
            "epoch_utc": channel._iso_ticks(epoch_ticks),
            "timeshift": (
                {
                    "enabled": True,
                    "start_param": ts_cfg.start_param,
                    "end_param": ts_cfg.end_param,
                    "continuous_param": ts_cfg.continuous_param,
                    "full_loop_param": ts_cfg.full_loop_param,
                    "max_span_seconds": ts_cfg.max_span_seconds,
                    "continuous_supported": channel.continuous or channel.continuous_error is None,
                }
                if ts_cfg.enabled
                else {"enabled": False}
            ),
        }

    @app.get(f"/{LoopPackage.INDEX_PLAYLIST}")
    def hls_master_playlist():
        ch, window, query = _request_view()
        return _manifest_response(
            ch.build_hls_master_playlist(query), "application/vnd.apple.mpegurl", window
        )

    @app.get(f"/{LoopPackage.DASH_MANIFEST}")
    def dash_manifest():
        ch, window, _query = _request_view()
        return _manifest_response(ch.build_dash_manifest(window=window), "application/dash+xml", window)

    def _register_video_playlist(rendition):
        def view():
            ch, window, _query = _request_view()
            body = ch.build_hls_manifest(rendition.name, window=window)
            return _manifest_response(body, "application/vnd.apple.mpegurl", window)

        name = package.video_playlist_name(rendition)
        app.add_url_rule(f"/{name}", endpoint=f"hls_{name}", view_func=view)

    for _rendition in package.video_renditions:
        _register_video_playlist(_rendition)

    @app.get("/<rendition_name>/init.mp4")
    def init_segment(rendition_name: str):
        try:
            rendition = package.rendition_by_name(rendition_name)
        except KeyError:
            abort(404)
        if rendition.self_initializing:
            # SCOPE.md §11: a sparse rendition's segments carry their own
            # moov -- there is no shared init to serve. The manifest never
            # advertises this URI in that case (see build_hls_manifest's
            # `no_init` handling), so a real player should never hit this;
            # 404 defensively rather than raising.
            abort(404)
        return send_file(rendition.init_path())

    @app.get("/<rendition_name>/init_<int:span>.mp4")
    def span_init_segment(rendition_name: str, span: int):
        try:
            rendition = package.rendition_by_name(rendition_name)
        except KeyError:
            abort(404)
        path = rendition.init_path_for_span(span) if rendition.shared_init else None
        if path is None:
            abort(404)
        return send_file(path)

    _SEG_DEFAULTS = {"mode": "local", "origin_loop": None}

    @app.get("/<rendition_name>/seg/<int:physical_index>.m4s", defaults=_SEG_DEFAULTS)
    @app.get("/<rendition_name>/cseg/<int:physical_index>.m4s", defaults={"mode": "global", "origin_loop": None})
    def segment(rendition_name: str, physical_index: int, mode: str, origin_loop: int | None):
        # SCOPE.md §13.5: /seg/ is always the loop-local physical index;
        # /cseg/ always the global index (continuity shift, §12.2).
        local_index, shift_ticks = _decode_segment_index(physical_index, mode, origin_loop)
        try:
            path = channel.segment_bytes_path(rendition_name, local_index)
        except (KeyError, IndexError):
            abort(404)
        # SCOPE.md §11.3: a sparse-mode index with no physical media 404s --
        # manifest generation is otherwise completely unaffected (the
        # manifest always advertised this segment as if it existed).
        return _serve_segment(
            path, "video/iso.segment", ts=False, shift_ticks=shift_ticks, sequence_number=physical_index
        )

    @app.get("/<rendition_name>/seg/<int:physical_index>.ts", defaults=_SEG_DEFAULTS)
    @app.get("/<rendition_name>/cseg/<int:physical_index>.ts", defaults={"mode": "global", "origin_loop": None})
    @app.get("/<rendition_name>/rseg/<int:origin_loop>/<int:physical_index>.ts", defaults={"mode": "origin"})
    def hls_ts_segment(rendition_name: str, physical_index: int, mode: str, origin_loop: int | None):
        if package.hls_format != "ts":
            abort(404)
        local_index, shift_ticks = _decode_segment_index(physical_index, mode, origin_loop)
        try:
            path = channel.hls_ts_segment_path(rendition_name, local_index)
        except (KeyError, IndexError):
            abort(404)
        return _serve_segment(path, "video/mp2t", ts=True, shift_ticks=shift_ticks)

    if package.has_audio:
        @app.get(f"/{LoopPackage.AUDIO_PLAYLIST}")
        def hls_audio_manifest():
            if package.hls_format == "ts" and package.hls_ts_mux_audio:
                abort(404)
            ch, window, _query = _request_view()
            body = ch.build_hls_audio_manifest(window=window)
            return _manifest_response(body, "application/vnd.apple.mpegurl", window)

        @app.get("/audio/init.mp4")
        def audio_init_segment():
            if package.audio_rendition.audio_sparse:
                abort(404)
            return send_file(package.audio_rendition.audio_init_path())

        @app.get("/audio/init_<int:span>.mp4")
        def audio_span_init_segment(span: int):
            path = package.audio_rendition.audio_init_path_for_span(span) if package.audio_rendition.audio_sparse else None
            if path is None:
                abort(404)
            return send_file(path)

        @app.get("/audio/seg/<int:physical_index>.m4s", defaults=_SEG_DEFAULTS)
        @app.get("/audio/cseg/<int:physical_index>.m4s", defaults={"mode": "global", "origin_loop": None})
        def audio_segment(physical_index: int, mode: str, origin_loop: int | None):
            local_index, shift_ticks = _decode_segment_index(physical_index, mode, origin_loop)
            if shift_ticks is not None:
                # tfdt is in the audio track's own timescale, the shift in the package's.
                audio_ts = package.audio_rendition.audio_timescale(package.timescale)
                shift_ticks = round(shift_ticks * audio_ts / package.timescale)
            try:
                path = channel.audio_segment_bytes_path(local_index)
            except IndexError:
                abort(404)
            # sparse audio hole (path is None) -- _serve_segment 404s it.
            return _serve_segment(
                path, "audio/iso.segment", ts=False, shift_ticks=shift_ticks, sequence_number=physical_index
            )

        @app.get("/audio/seg/<int:physical_index>.ts", defaults=_SEG_DEFAULTS)
        @app.get("/audio/cseg/<int:physical_index>.ts", defaults={"mode": "global", "origin_loop": None})
        @app.get("/audio/rseg/<int:origin_loop>/<int:physical_index>.ts", defaults={"mode": "origin"})
        def hls_ts_audio_segment(physical_index: int, mode: str, origin_loop: int | None):
            if package.hls_format != "ts" or package.hls_ts_mux_audio:
                abort(404)
            local_index, shift_ticks = _decode_segment_index(physical_index, mode, origin_loop)
            if local_index >= package.segments_per_loop:
                abort(404)
            path = package.package_dir / "hls-ts" / "audio" / f"{local_index}.ts"
            if not path.is_file():  # sparse audio hole
                path = None
            return _serve_segment(path, "video/mp2t", ts=True, shift_ticks=shift_ticks)

    return app


def read_package_descriptor(package_dir: Path) -> dict:
    return json.loads((package_dir / "loop_descriptor.json").read_text())


def resolve_epoch_ticks(epoch_utc: str, timescale: int) -> int:
    """Convert an ISO8601 UTC epoch string to an integer tick count --
    callers should do this exactly once and feed the result into every
    subsequent request's arithmetic, never recompute it via accumulation."""
    epoch_dt = _dt.datetime.strptime(epoch_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=_dt.timezone.utc
    )
    return round(epoch_dt.timestamp() * timescale)


def resolve_window_segments(
    package_descriptor: dict,
    *,
    dvr_window_seconds: float = 30.0,
    window_segments: int | None = None,
) -> int:
    """Exact segment count if given, else derived from a DVR-window target
    using the package's nominal segment duration from bake time."""
    if window_segments is not None:
        return window_segments
    nominal_segment_duration_seconds = float(package_descriptor["segment_duration_seconds"])
    return max(1, math.ceil(dvr_window_seconds / nominal_segment_duration_seconds))


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve a loop package as live HLS/DASH")
    parser.add_argument("package_dir", type=Path, help="Path to a baked loop package directory")
    parser.add_argument(
        "--epoch-utc",
        type=str,
        required=True,
        help="Channel epoch, ISO8601 UTC (e.g. 2026-01-01T00:00:00Z). loop 0 starts here.",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--dvr-window-seconds",
        type=float,
        default=30.0,
        help="Approximate size of the DVR window / sliding manifest, in "
        "seconds (default: 30). Converted to a segment count using the "
        "package's nominal --segment-duration from bake time. Ignored if "
        "--window-segments is also given.",
    )
    parser.add_argument(
        "--window-segments",
        type=int,
        default=None,
        help="Exact number of segments to advertise per manifest response "
        "(HLS sliding window / DASH SegmentTimeline). Overrides "
        "--dvr-window-seconds if given.",
    )
    parser.add_argument(
        "--continuous-timeline",
        action="store_true",
        help="SCOPE.md §12: rewrite every segment's internal PTS/DTS/PCR "
        "(TS) or tfdt (CMAF) per request so the channel is one genuinely "
        "continuous timeline with no #EXT-X-DISCONTINUITY / DASH Period "
        "restart at the loop wrap. Requires a package with no internal "
        "asset-boundary discontinuities and 64-bit (v1) tfdt CMAF "
        "fragments -- checked once at startup, hard-fails otherwise.",
    )
    parser.add_argument(
        "--timeshift",
        action="store_true",
        help="SCOPE.md §13: enable startover/catchup via query parameters on "
        "the normal manifest URLs.",
    )
    parser.add_argument("--timeshift-start-param", default="start")
    parser.add_argument("--timeshift-end-param", default="end")
    parser.add_argument("--timeshift-continuous-param", default="continuous_timeline")
    parser.add_argument("--timeshift-full-loop-param", default="full_loop")
    parser.add_argument("--timeshift-max-span-seconds", type=int, default=21600)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    package_descriptor = read_package_descriptor(args.package_dir)
    timescale = int(package_descriptor["timescale"])
    epoch_ticks = resolve_epoch_ticks(args.epoch_utc, timescale)
    window_segments = resolve_window_segments(
        package_descriptor,
        dvr_window_seconds=args.dvr_window_seconds,
        window_segments=args.window_segments,
    )

    logger.info(
        "DVR window: %d segment(s) (~%.1fs nominal, requested %.1fs)",
        window_segments,
        window_segments * float(package_descriptor["segment_duration_seconds"]),
        args.dvr_window_seconds,
    )

    logger.warning(
        "Running Flask's built-in dev server -- single-threaded, serializes "
        "concurrent requests. Fine for local dev/testing and local-docker; "
        "the ecs-express Docker image's production path fronts this app "
        "with gunicorn instead (see wsgi.py, docker-entrypoint.sh, PERFS.md)."
    )
    app = create_app(
        args.package_dir,
        epoch_ticks,
        window_segments=window_segments,
        continuous=args.continuous_timeline,
        timeshift=TimeshiftConfig(
            enabled=args.timeshift,
            start_param=args.timeshift_start_param,
            end_param=args.timeshift_end_param,
            continuous_param=args.timeshift_continuous_param,
            full_loop_param=args.timeshift_full_loop_param,
            max_span_seconds=args.timeshift_max_span_seconds,
        ),
    )
    app.run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
