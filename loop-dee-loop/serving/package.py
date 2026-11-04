"""A baked loop package as loaded from disk: renditions, the audio track, markers, boundaries and
the chunk/part tables. Read-only, loaded once at process start."""

from __future__ import annotations

import json
import math
from pathlib import Path

import cmaf
import lowlatency
import package_io
from scte35_signaling import build_cue_breaks

from serving.timeline import (
    compute_asset_boundary_set,
    compute_declared_offset_ticks_by_local_index,
)


def _numeric_segment_index(path: Path) -> int:
    stem = path.stem
    tail = stem.rsplit("_", 1)[-1]
    return int(tail)


class VideoRendition:
    """Read-only view over one rendition's segments within a loop package
    (`<package_dir>/segments/<name>/`). At most one rendition also carries
    the shared audio track (see `has_audio`)."""

    def __init__(self, package_dir: Path, rendition: dict):
        self.name: str = rendition["name"]
        # The longest baked chunk, in package ticks (bake.py --chunk-duration; 0 = not a chunked bake).
        self.max_chunk_ticks: int = int(rendition.get("max_chunk_ticks") or 0)
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
                found = cmaf.track_timescale(package_io.read_bytes(init)) if init else None
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
        # Set only by `bake.py --chunk-duration`: segments hold several
        # moof+mdat chunks, so low-latency DASH delivery is possible.
        _chunk = self.descriptor.get("chunk_duration_seconds")
        self.chunk_duration_seconds: float | None = float(_chunk) if _chunk else None
        self.markers: list[dict] = self.descriptor["markers"]
        # Name of the baked source (franken-ts output name), when recorded.
        source_input = self.descriptor.get("source_input")
        self.source_name: str = ""
        if source_input:
            source_path = Path(source_input)
            self.source_name = source_path.stem if source_path.suffix in (".ts", ".json") else source_path.name

        # [markers] settings (its-a-live/AGENTS.md) -- fixed shape of this
        # package's HLS/DASH SCTE-35 signaling, recorded once at bake time
        # (bake.py) and simply read back here; defaults match bake.py's own
        # defaults, for loop packages baked before these settings existed.
        # bake.py (>= 2.0) stores both SCTE-35 messages per marker; an older package has
        # only the one its --daterange-mode chose, so its payload cannot be switched.
        self.has_both_scte35_payloads: bool = self.descriptor.get("scte35_payloads") == "shared+narrowed"
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

        # The full ladder, in master-playlist order: media playlist names
        # (video_N.m3u8) are positions in it, so a per-request rendition
        # filter (Channel.with_rendition_filter) must not renumber them.
        self.all_video_renditions: list[VideoRendition] = list(self.video_renditions)

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
            separate_ts_audio = self.has_audio and not self.hls_ts_mux_audio and not self.audio_rendition.audio_sparse
            if separate_ts_audio and len(list((package_dir / "hls-ts" / "audio").glob("*.ts"))) != self.segments_per_loop:
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
        self.max_segment_duration_seconds_rounded_up = math.ceil(max_duration_seconds)

    @property
    def has_audio(self) -> bool:
        return self.audio_rendition is not None

    # ── Public playlist names ──────────────────────────────────────────────
    # index.m3u8 (HLS multivariant), index.mpd (DASH), audio.m3u8 (the single
    # shared audio track), and one media playlist per video rendition:
    # video[_N].m3u8 (video only) or video_audio[_N].m3u8 (audio muxed into the
    # segments, i.e. hls_format=ts with hls_ts_mux_audio). N is the 1-based
    # rendition index, only present when there is more than one rendition.
    INDEX_PLAYLIST = "index.m3u8"
    AUDIO_PLAYLIST = "audio.m3u8"
    DASH_MANIFEST = "index.mpd"
    # Legacy / alternate names, served as aliases of the two entry points above.
    HLS_ALIAS = "stream.m3u8"
    DASH_ALIAS = "stream.mpd"

    @property
    def audio_muxed_in_video(self) -> bool:
        return self.has_audio and self.hls_format == "ts" and self.hls_ts_mux_audio

    def video_playlist_name(self, rendition: VideoRendition) -> str:
        base = "video_audio" if self.audio_muxed_in_video else "video"
        position = self.all_video_renditions.index(rendition) + 1
        suffix = f"_{position}" if len(self.all_video_renditions) > 1 else ""
        return f"{base}{suffix}.m3u8"

    def rendition_by_name(self, name: str) -> VideoRendition:
        for r in self.video_renditions:
            if r.name == name:
                return r
        raise KeyError(name)

    # ── low latency: the parts of a segment ────────────────────────────────

    @property
    def part_target_seconds(self) -> float | None:
        """LL-HLS `PART-TARGET`: at least the longest baked chunk, rounded up to a millisecond (it must cover every
        part and never change while the channel runs). None for a package baked without chunks."""
        longest = max((r.max_chunk_ticks for r in self.video_renditions), default=0)
        if not longest:
            return None
        return math.ceil(longest * 1000 / self.timescale) / 1000

    def video_track_timescale(self, rendition: VideoRendition) -> int:
        """The video track's own media timescale (its init's mdhd), which its `tfdt`s are written in."""
        cache = self.__dict__.setdefault("_video_timescales", {})
        if rendition.name not in cache:
            found = None
            if not rendition.self_initializing:
                try:
                    found = cmaf.track_timescale(package_io.read_bytes(rendition.init_path()))
                except (OSError, RuntimeError):
                    found = None
            cache[rendition.name] = found or self.timescale
        return cache[rendition.name]

    def chunk_table(self, rendition_name: str | None, local_index: int, duration_ticks: int) -> list[lowlatency.Chunk]:
        """The chunks (LL-HLS parts) of one baked segment; `rendition_name` None = the audio track. Read lazily from
        the segment file and cached: the files are immutable, so the cache never goes stale and a package baked
        before LL-HLS existed needs no re-bake."""
        cache = self.__dict__.setdefault("_chunk_tables", {})
        key = (rendition_name, local_index)
        if key not in cache:
            if rendition_name is None:
                audio = self.audio_rendition
                path = audio.audio_segment_path_for_index(local_index)
                timescale, all_sync = audio.audio_timescale(self.timescale), True
            else:
                rendition = self.rendition_by_name(rendition_name)
                path = rendition.segment_path_for_index(local_index)
                timescale, all_sync = self.video_track_timescale(rendition), False
            cache[key] = (
                [] if path is None
                else lowlatency.chunk_table(package_io.read_bytes(path), timescale, self.timescale, duration_ticks, all_sync=all_sync)
            )
        return cache[key]


def _first_present_segment(files: list[Path | None], label: str) -> Path:
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
    data = package_io.read_bytes(path)
    moof = cmaf._find(data, ("moof",))
    tfdt = cmaf._find(data, ("traf", "tfdt"), moof[0] + 8, moof[1]) if moof else None
    if moof is None or tfdt is None:
        raise RuntimeError(f"{path}: no moof/traf/tfdt box found -- not a valid CMAF media fragment")
    return data[tfdt[0] + 8]
