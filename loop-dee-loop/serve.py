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
import dataclasses
import datetime as _dt
import json
import logging
import math
import time
from collections.abc import Callable
from pathlib import Path

from flask import Flask, Response, abort, send_file

from loop_math import (
    compute_loop_position,
    global_segment_number,
    program_date_time_ticks,
    segment_index_for_position,
    ticks_to_wall_clock_seconds,
)
from scte35_signaling import (
    SignalingMarker,
    build_cue_breaks,
    build_cue_in_tag,
    build_cue_out_cont_tag,
    build_cue_out_tag,
    build_daterange_tags,
    build_event_id_map,
    build_grouped_daterange_tags,
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
        raw_asset_boundaries = self.descriptor.get("asset_boundaries") or []
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


class Channel:
    """Fixed channel epoch + loop package. All request handling goes through
    this class's stateless methods -- no attribute here is ever mutated
    after construction."""

    def __init__(self, package: LoopPackage, epoch_ticks: int, window_segments: int = 6):
        if not isinstance(epoch_ticks, int):
            raise ValueError("epoch_ticks must be int")
        if window_segments < 1:
            raise ValueError("window_segments must be >= 1")
        self.package = package
        self.epoch_ticks = epoch_ticks
        # Controls the DVR window / manifest size: how many segments ahead
        # of the live edge are advertised in each manifest response (HLS
        # sliding window, DASH SegmentTimeline + timeShiftBufferDepth).
        # Larger = more seekable-back history for players, larger manifest
        # responses. See --dvr-window-seconds / --window-segments in
        # serve.py's CLI.
        self.window_segments = window_segments

    def now_ticks(self) -> int:
        """The only place wall-clock time is sampled. Converted to an
        integer tick count exactly once, at the point of measurement."""
        return round(time.time() * self.package.timescale)

    def current_position(self):
        return compute_loop_position(
            self.now_ticks(), self.epoch_ticks, self.package.total_loop_duration_ticks
        )

    def build_hls_master_playlist(self) -> str:
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
                    'DEFAULT=YES,AUTOSELECT=YES,URI="audio.m3u8"'
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
            lines.append(pkg.video_playlist_name(rendition))

        return "\n".join(lines) + "\n"

    def build_hls_manifest(self, rendition_name: str, window_segments: int | None = None) -> str:
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
        )

    def build_hls_audio_manifest(self, window_segments: int | None = None) -> str:
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
        )

    def _build_hls_media_playlist(
        self,
        *,
        boundary_ticks: list[int],
        init_uri: str | None,
        seg_uri_template: str,
        window_segments: int | None = None,
        span_init_uri: Callable[[int], str] | None = None,
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
        pos = self.current_position()
        pkg = self.package
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
        first_global_index = max(0, media_sequence - window_segments + 1)
        # grave-robber/SCOPE.md §6.1: generalizes the original "one
        # discontinuity per loop wrap" counter (previously just
        # `first_global_index // segments_per_loop`) to also count internal
        # asset-boundary discontinuities -- reduces to exactly that formula
        # when pkg.boundaries == {0} (see compute_discontinuity_sequence).
        first_discontinuity_sequence = compute_discontinuity_sequence(
            first_global_index, pkg.segments_per_loop, pkg.boundaries
        )

        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:6" if init_uri is None and span_init_uri is None else "#EXT-X-VERSION:7",
            f"#EXT-X-TARGETDURATION:{pkg.max_segment_duration_seconds_rounded_up}",
            f"#EXT-X-MEDIA-SEQUENCE:{first_global_index}",
            f"#EXT-X-DISCONTINUITY-SEQUENCE:{first_discontinuity_sequence}",
        ]
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

            if i > 0 and local_index in pkg.boundaries:
                # This segment starts a new loop iteration OR an internal
                # asset-boundary join (grave-robber/SCOPE.md §6.1), and
                # isn't the very first entry in the window (whose implicit
                # discontinuity sequence is already covered by the header
                # above) -- signal the timestamp discontinuity here.
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
            lines.append(seg_uri_template.format(index=local_index))

        return "\n".join(lines) + "\n"

    def build_dash_manifest(self, window_segments: int | None = None) -> str:
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
        window_segments = window_segments or self.window_segments
        pos = self.current_position()
        pkg = self.package
        seg_index = segment_index_for_position(
            pos.position_in_loop_ticks, pkg.segment_boundary_ticks
        )
        current_loop_number = pos.loop_number

        epoch_seconds = ticks_to_wall_clock_seconds(self.epoch_ticks, pkg.timescale)
        availability_start_time = (
            _dt.datetime.utcfromtimestamp(epoch_seconds).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            + "Z"
        )
        now_seconds = ticks_to_wall_clock_seconds(self.now_ticks(), pkg.timescale)
        publish_time = (
            _dt.datetime.utcfromtimestamp(now_seconds).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            + "Z"
        )

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

        current_span_index = _span_index_for_local(seg_index)
        current_span_start, _ = _span_bounds(current_span_index)
        # Segments served so far in the CURRENT open span -- never
        # front-pruned (same DASH-client requirement as before, now scoped
        # to the span rather than the whole loop).
        open_count = seg_index - current_span_start + 1

        periods_plan: list[tuple[int, int, list[int]]] = []
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

        period_xml_parts = []
        for loop_number, span_index, local_indices in periods_plan:
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

            # `id` is the real, plain event_id (as an actual int, matching
            # SCTE-35's own segmentation_event_id / the channel config's
            # `event_id` -- never a compound string): a player decodes the
            # <Binary> payload itself (via the `scte35` npm package, same
            # as HLS -- see igor's scte35Lite.ts describeAllMarkers()) to
            # get the segmentation/splice type, rather than us encoding it
            # into `id`.
            #
            # A Start/End pair SHARES one event_id by design (see the
            # module docstring on event_id reuse), and once enough of a
            # loop iteration has played out that BOTH halves sit inside the
            # currently-open Period's never-pruned segment range (see the
            # comment above `periods_plan`), they're both due in the SAME
            # <EventStream> -- if `id` alone had to disambiguate them,
            # dash.js's EventController (confirmed against its own source)
            # would see the second one as a duplicate of the first
            # (same id) and silently drop it, so the End marker would
            # never fire. Likewise the exact same marker recurring next
            # loop, with the exact same id, needs to be recognized as a
            # NEW occurrence, not a dup of the one already scheduled --
            # EventController's dedupe key isn't `id` alone though, it's
            # `(EventStream@value, id)` (`(!value || eventStream.value ===
            # value) && e.id === id`), so both problems are solved the
            # DASH-native way: by grouping Events into separate
            # <EventStream> elements whose own `@value` differs per
            # (loop_number, start/end/instant) -- never by smuggling that
            # information into `id`.
            # [markers].increment_event_ids: one id map per Period, keyed
            # by that Period's own loop_number (same helper HLS uses, see
            # _remap_signaling_markers) -- {} when the setting is off, so
            # the lookups below become no-ops via dict.get() fallback.
            event_id_map = build_event_id_map(pkg.markers, loop_number) if pkg.increment_event_ids else {}

            event_xml_by_stream: dict[str, list[str]] = {}
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
                splice_command_b64 = (
                    reencode_event_ids(marker["splice_command_b64"], event_id_map)
                    if pkg.increment_event_ids
                    else marker["splice_command_b64"]
                )
                if is_instant_segmentation(marker):
                    direction = "instant"
                elif is_out_marker(marker):
                    direction = "out"
                else:
                    direction = "in"
                # Unchanged shape when there's exactly one span per loop
                # (the common, no-internal-asset-boundary case) -- only
                # disambiguated by span_index too when grave-robber/
                # SCOPE.md §6.1 internal boundaries make more than one
                # Period share the same loop_number, so dash.js's
                # (EventStream@value, id) dedupe key doesn't fold events
                # from two different Periods together.
                stream_value = (
                    f"{loop_number}-{direction}"
                    if spans_per_loop == 1
                    else f"{loop_number}-{span_index}-{direction}"
                )
                # Period-relative, like <S t=...> -- subtract this Period's
                # own start tick (grave-robber/SCOPE.md §6.2's declared
                # offset lives in the Period's start= instead, see above; a
                # no-op subtraction of pkg.segment_boundary_ticks[0]==0 in
                # the common single-span-per-loop case).
                event_presentation_time = (
                    marker["pts_time_ticks"] - pkg.segment_boundary_ticks[span_start_local]
                )
                event_xml_by_stream.setdefault(stream_value, []).append(
                    f'    <Event presentationTime="{event_presentation_time}"'
                    f'{duration_attr} id="{event_id_dec}">\n'
                    f'      <Signal xmlns="urn:scte:scte35:2013:xml">\n'
                    f'        <Binary>{splice_command_b64}</Binary>\n'
                    f"      </Signal>\n"
                    f"    </Event>"
                )

            video_representations = []
            for idx, rendition in enumerate(pkg.video_renditions):
                entries = _period_entries(
                    rendition.segment_boundary_ticks, local_indices, span_start_local
                )
                timeline_lines = "\n".join(
                    f'        <S t="{t}" d="{d}" />' for t, d, _ in entries
                )
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
                         timescale="{pkg.timescale}" startNumber="{first_number}">
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
                audio_timeline_lines = "\n".join(
                    f'        <S t="{t}" d="{d}" />' for t, d, _ in audio_entries
                )
                audio_adaptation_set = f'''
    <AdaptationSet mimeType="audio/mp4" segmentAlignment="true" startWithSAP="1">
      <Representation id="a0" bandwidth="{a["bandwidth"]}" codecs="{a["codecs"]}">
        <SegmentTemplate media="audio/seg/$Number$.m4s" initialization="{audio_init}"
                         timescale="{pkg.timescale}" startNumber="{first_number}">
          <SegmentTimeline>
{audio_timeline_lines}
          </SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>'''

            event_streams_xml = "\n".join(
                f'    <EventStream schemeIdUri="urn:scte:scte35:2014:xml+bin" '
                f'timescale="{pkg.timescale}" value="{stream_value}">\n'
                + "\n".join(events)
                + "\n    </EventStream>"
                for stream_value, events in event_xml_by_stream.items()
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
        mpd = f'''<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"
     profiles="urn:mpeg:dash:profile:isoff-live:2011"
     type="dynamic"
     availabilityStartTime="{availability_start_time}"
     publishTime="{publish_time}"
     minimumUpdatePeriod="PT{pkg.max_segment_duration_seconds_rounded_up}S"
     timeShiftBufferDepth="PT{pkg.max_segment_duration_seconds_rounded_up * window_segments}S"
     suggestedPresentationDelay="PT{pkg.max_segment_duration_seconds_rounded_up * 2}S"
     minBufferTime="PT2S">
{chr(10).join(period_xml_parts)}
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


def create_app(package_dir: Path, epoch_ticks: int, window_segments: int = 6) -> Flask:
    package = LoopPackage(package_dir)
    channel = Channel(package, epoch_ticks, window_segments=window_segments)
    process_start_ticks = channel.now_ticks()

    app = Flask(__name__)

    @app.after_request
    def _add_cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

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
        }

    @app.get(f"/{LoopPackage.INDEX_PLAYLIST}")
    def hls_master_playlist():
        body = channel.build_hls_master_playlist()
        return Response(body, mimetype="application/vnd.apple.mpegurl")

    @app.get(f"/{LoopPackage.DASH_MANIFEST}")
    def dash_manifest():
        body = channel.build_dash_manifest()
        return Response(body, mimetype="application/dash+xml")

    def _register_video_playlist(rendition):
        def view():
            body = channel.build_hls_manifest(rendition.name)
            return Response(body, mimetype="application/vnd.apple.mpegurl")

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

    @app.get("/<rendition_name>/seg/<int:physical_index>.m4s")
    def segment(rendition_name: str, physical_index: int):
        try:
            path = channel.segment_bytes_path(rendition_name, physical_index)
        except (KeyError, IndexError):
            abort(404)
        # SCOPE.md §11.3: a sparse-mode index with no physical media 404s --
        # manifest generation is otherwise completely unaffected (the
        # manifest always advertised this segment as if it existed).
        if path is None:
            abort(404)
        return send_file(path, mimetype="video/iso.segment")

    @app.get("/<rendition_name>/seg/<int:physical_index>.ts")
    def hls_ts_segment(rendition_name: str, physical_index: int):
        if package.hls_format != "ts":
            abort(404)
        try:
            path = channel.hls_ts_segment_path(rendition_name, physical_index)
        except (KeyError, IndexError):
            abort(404)
        if path is None:
            abort(404)
        return send_file(path, mimetype="video/mp2t")

    if package.has_audio:
        @app.get(f"/{LoopPackage.AUDIO_PLAYLIST}")
        def hls_audio_manifest():
            if package.hls_format == "ts" and package.hls_ts_mux_audio:
                abort(404)
            body = channel.build_hls_audio_manifest()
            return Response(body, mimetype="application/vnd.apple.mpegurl")

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

        @app.get("/audio/seg/<int:physical_index>.m4s")
        def audio_segment(physical_index: int):
            try:
                path = channel.audio_segment_bytes_path(physical_index)
            except IndexError:
                abort(404)
            if path is None:  # sparse audio hole
                abort(404)
            return send_file(path, mimetype="audio/iso.segment")

        @app.get("/audio/seg/<int:physical_index>.ts")
        def hls_ts_audio_segment(physical_index: int):
            if package.hls_format != "ts" or package.hls_ts_mux_audio or physical_index >= package.segments_per_loop:
                abort(404)
            path = package.package_dir / "hls-ts" / "audio" / f"{physical_index}.ts"
            if not path.is_file():  # sparse audio hole
                abort(404)
            return send_file(path, mimetype="video/mp2t")

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
    app = create_app(args.package_dir, epoch_ticks, window_segments=window_segments)
    app.run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
