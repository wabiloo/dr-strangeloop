from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .utils import parse_time

TimeValue = Union[str, int, float]


class RenditionConfig(BaseModel):
    """One entry in a multi-rendition ABR ladder (see OutputConfig.renditions).

    Each rendition shares the same timeline/ad-break schedule and produces
    its own <output.dir>/<name>.ts, all sharing a single markers.json (PTS
    values are identical across renditions by construction -- see
    loop-dee-loop's SCOPE.md discussion on cross-rendition keyframe
    alignment). `name` becomes both the output filename stem and the
    rendition's identity downstream (loop-dee-loop URLs/manifest labels).
    """

    name: str
    resolution: str
    bitrate_kbps: int

    @property
    def width(self) -> int:
        return int(self.resolution.split("x")[0])

    @property
    def height(self) -> int:
        return int(self.resolution.split("x")[1])


class OutputConfig(BaseModel):
    file: Optional[Path] = None
    dir: Optional[Path] = None
    renditions: Optional[list[RenditionConfig]] = None
    resolution: str = "1920x1080"
    framerate: int = 25
    bitrate_kbps: int = 10000
    gop: Optional[int] = None
    service_provider: str = "broadpeak"
    service_name: str = "broadpeak.io"

    @model_validator(mode="after")
    def default_gop(self) -> "OutputConfig":
        if self.gop is None:
            self.gop = self.framerate * 2
        return self

    @model_validator(mode="after")
    def validate_single_vs_multi_rendition(self) -> "OutputConfig":
        """Exactly one of two mutually exclusive modes:

        - single-rendition (legacy): `file` set, `dir`/`renditions` unset.
          Writes `<file>` + `<file-with-.markers.json-suffix>`, unchanged
          from before this feature existed.
        - multi-rendition: `dir` + `renditions` (non-empty) set, `file`
          unset. Writes `<dir>/<rendition.name>.ts` per rendition +
          `<dir>/markers.json` (shared, written once).
        """
        has_file = self.file is not None
        has_ladder = self.dir is not None or self.renditions is not None

        if has_file and has_ladder:
            raise ValueError(
                "output: specify either 'file' (single-rendition) or "
                "'dir' + 'renditions' (multi-rendition), not both"
            )
        if not has_file and not has_ladder:
            raise ValueError("output: must specify either 'file' or 'dir' + 'renditions'")
        if has_ladder:
            if self.dir is None or self.renditions is None or len(self.renditions) == 0:
                raise ValueError(
                    "output: multi-rendition mode requires both 'dir' and a "
                    "non-empty 'renditions' list"
                )
            names = [r.name for r in self.renditions]
            if len(names) != len(set(names)):
                raise ValueError(f"output.renditions: duplicate rendition name(s) in {names}")
        return self

    @property
    def is_multi_rendition(self) -> bool:
        return self.renditions is not None

    def for_rendition(self, rendition: "RenditionConfig") -> "OutputConfig":
        """Return an effective single-output OutputConfig for one rendition
        of a multi-rendition ladder: same framerate/gop/service metadata,
        resolution/bitrate_kbps/file overridden from the rendition, dir/
        renditions cleared. This lets extract.py/validate.py/cache.py --
        all of which only ever look at a single OutputConfig -- work
        completely unchanged, whether we're in single- or multi-rendition
        mode; only cli.py's orchestration loop needs to know renditions
        exist at all.
        """
        assert self.dir is not None
        return self.model_copy(
            update={
                "file": self.dir / f"{rendition.name}.ts",
                "resolution": rendition.resolution,
                "bitrate_kbps": rendition.bitrate_kbps,
                "dir": None,
                "renditions": None,
            }
        )

    @property
    def width(self) -> int:
        return int(self.resolution.split("x")[0])

    @property
    def height(self) -> int:
        return int(self.resolution.split("x")[1])


class SegmentationConfig(BaseModel):
    type_id: str = "0x34"
    upid_type: str = "0x09"
    upid_hex: str = ""
    web_delivery_allowed: bool = True
    no_regional_blackout: bool = False
    archive_allowed: bool = False
    device_restrictions: int = 1
    duration: Optional[TimeValue] = None
    # Sibling position within an enclosing marker (e.g. the Nth of M Provider
    # Placement Opportunities inside a Break).  Left unset by the user in the
    # common case: the `markers` resolution pass (see timeline.py) fills these
    # in automatically from inferred containment, but an explicit value here
    # always wins.
    segment_num: Optional[int] = None
    segments_expected: Optional[int] = None

    def duration_seconds(self) -> Optional[float]:
        if self.duration is None:
            return None
        return parse_time(self.duration)


class SpliceConfig(BaseModel):
    """Shared SCTE-35 splice-signaling fields, used by `MarkerConfig`."""

    event_id: int
    splice_type: Literal["splice_insert", "time_signal"] = "splice_insert"
    unique_program_id: str = "0x0001"
    avail_num: int = 18
    avails_expected: int = 255
    provider_avail_id: str = "0x00000012"
    # `splice_insert` only: whether to attach the legacy splice_avail_descriptor
    # (tag 0) to each splice_insert message. Set false for a bare splice_insert
    # with no descriptor loop at all.
    descriptors: bool = True
    # `splice_insert` only: whether the receiver should return to network on
    # its own once `break_duration` elapses (SCTE-35 `auto_return`), vs. an
    # explicit second `splice_insert` (out_of_network=false) marking the real
    # return point. `True` (the default) emits a single message and no
    # cue-in; `False` emits the out/in pair, matching `time_signal`'s
    # start/stop signaling.
    auto_return: bool = True
    segmentation: Optional[SegmentationConfig] = None

    @model_validator(mode="after")
    def segmentation_required_for_time_signal(self) -> "SpliceConfig":
        if self.splice_type == "time_signal" and self.segmentation is None:
            raise ValueError("'segmentation' is required when splice_type is 'time_signal'")
        return self


# SCTE-35 Table 22 segmentation_type_id (Start value) -> `type` lane label.
# `type` is purely a downstream/UI convenience (timeline lane grouping,
# markers.json labeling) -- `segmentation.type_id` is the single source of
# truth for what a marker actually signals, so `type` is always *derived*
# from it (never the other way around -- that's what let them silently
# disagree: a marker authored/edited to say `type: break` while its
# `segmentation.type_id` was left at an unrelated PPO value).
LANE_FOR_SEGMENTATION_TYPE_ID: dict[str, str] = {
    "0x22": "break",   # Break
    "0x34": "ppo",      # Provider Placement Opportunity
    "0x36": "ppo",      # Distributor Placement Opportunity
    "0x38": "ppo",      # Provider Overlay Placement Opportunity
    "0x3A": "ppo",      # Distributor Overlay Placement Opportunity
    "0x30": "ad",       # Provider Advertisement
    "0x32": "ad",       # Distributor Advertisement
    "0x3C": "ad",       # Provider Promo
    "0x3E": "ad",       # Distributor Promo
    "0x44": "ad",       # Provider Ad Block
    "0x46": "ad",       # Distributor Ad Block
}


# SCTE-35 Table 22 segmentation_type_id (Start value) -> bare name (no
# "Start"/"End" wording). Mirrors igor/frontend/src/segmentationPresets.ts's
# SEGMENTATION_PAIR_OPTIONS[].name exactly, so the CLI-burned-in OSD
# abbreviations (see osd.py's abbreviation_for_marker) and igor's UI never
# describe the same type_id differently. Used only for OSD display -- not a
# source of truth for `type`/lane grouping (that's LANE_FOR_SEGMENTATION_TYPE_ID
# above).
SEGMENTATION_TYPE_NAME: dict[str, str] = {
    "0x00": "Not Indicated",
    "0x01": "Content Identification",
    "0x02": "Call Ad Server",
    "0x10": "Program",
    "0x12": "Program Early Termination",
    "0x13": "Program Breakaway",
    "0x14": "Program Resumption",
    "0x15": "Program Runover Planned",
    "0x16": "Program Runover Unplanned",
    "0x17": "Program Overlap Start",
    "0x18": "Program Blackout Override",
    "0x19": "Program Start -- In Progress",
    "0x20": "Chapter",
    "0x22": "Break",
    "0x24": "Opening Credit",
    "0x26": "Closing Credit",
    "0x30": "Provider Advertisement",
    "0x32": "Distributor Advertisement",
    "0x34": "Provider Placement Opportunity",
    "0x36": "Distributor Placement Opportunity",
    "0x38": "Provider Overlay Placement Opportunity",
    "0x3A": "Distributor Overlay Placement Opportunity",
    "0x3C": "Provider Promo",
    "0x3E": "Distributor Promo",
    "0x40": "Unscheduled Event",
    "0x42": "Alternate Content Opportunity",
    "0x44": "Provider Ad Block",
    "0x46": "Distributor Ad Block",
    "0x50": "Network",
}

# Stable three-letter codes for compact SCTE-35 span labels (OSD overlays,
# reports, and other consumers). Keep these explicit rather than deriving
# initials: several names collide or produce codes that are too long.
SEGMENTATION_TYPE_CODE: dict[str, str] = {
    "0x00": "NIN",  # Not Indicated
    "0x01": "CID",  # Content Identification
    "0x02": "CAS",  # Call Ad Server
    "0x10": "PRG",  # Program
    "0x12": "PET",  # Program Early Termination
    "0x13": "PBA",  # Program Breakaway
    "0x14": "PRS",  # Program Resumption
    "0x15": "PRP",  # Program Runover Planned
    "0x16": "PRU",  # Program Runover Unplanned
    "0x17": "POS",  # Program Overlap Start
    "0x18": "PBO",  # Program Blackout Override
    "0x19": "PIP",  # Program Start -- In Progress
    "0x20": "CHP",  # Chapter
    "0x22": "BRK",  # Break
    "0x24": "OPN",  # Opening Credit
    "0x26": "CLC",  # Closing Credit
    "0x30": "PAD",  # Provider Advertisement
    "0x32": "DAD",  # Distributor Advertisement
    "0x34": "PPO",  # Provider Placement Opportunity
    "0x36": "DPO",  # Distributor Placement Opportunity
    "0x38": "PVO",  # Provider Overlay Placement Opportunity
    "0x3A": "DVO",  # Distributor Overlay Placement Opportunity
    "0x3C": "PPR",  # Provider Promo
    "0x3E": "DPR",  # Distributor Promo
    "0x40": "USC",  # Unscheduled Event
    "0x42": "ACO",  # Alternate Content Opportunity
    "0x44": "PAB",  # Provider Ad Block
    "0x46": "DAB",  # Distributor Ad Block
    "0x50": "NET",  # Network
}


def lane_for_type_id(type_id: str | int) -> str:
    """The `type` lane (break/ppo/ad/custom) a segmentation_type_id belongs
    to -- "custom" for anything not in LANE_FOR_SEGMENTATION_TYPE_ID (e.g.
    Program/Chapter/Credit/Network/standalone-instant types)."""
    value = int(type_id, 16) if isinstance(type_id, str) else int(type_id)
    normalized = f"0x{value:02X}"
    return LANE_FOR_SEGMENTATION_TYPE_ID.get(normalized, "custom")

# SCTE-35 Table 22 segmentation_type_id values that are standalone/instant
# signals -- NOT part of a Start/End pair (there is no "+1" partner; e.g.
# 0x13 Program Breakaway is its own distinct type, not "0x12 End"). Every
# other type_id in the table is a Start (even) / End (odd) pair. Used to
# decide whether a `time_signal` marker resolves to one boundary (instant,
# at the marker span's start) or two (start + stop, spanning the marker).
INSTANT_SEGMENTATION_TYPE_IDS: frozenset[str] = frozenset({
    "0x00",  # Not Indicated
    "0x01",  # Content Identification
    "0x02",  # Call Ad Server
    "0x12",  # Program Early Termination
    "0x13",  # Program Breakaway
    "0x14",  # Program Resumption
    "0x15",  # Program Runover Planned
    "0x16",  # Program Runover Unplanned
    "0x17",  # Program Overlap Start
    "0x18",  # Program Blackout Override
    "0x19",  # Program Start -- In Progress
})


def is_instant_segmentation(segmentation: Optional["SegmentationConfig"]) -> bool:
    """True if `segmentation` describes a standalone/instant signal (see
    INSTANT_SEGMENTATION_TYPE_IDS) rather than a Start/End pair."""
    if segmentation is None:
        return False
    type_id = segmentation.type_id
    value = int(type_id, 16) if isinstance(type_id, str) else int(type_id)
    normalized = f"0x{value:02X}"
    return normalized in INSTANT_SEGMENTATION_TYPE_IDS


def _marker_signal_identity(marker: "MarkerConfig") -> tuple:
    """What (splice_type, segmentation_type_id) this marker actually signals
    -- two markers over the exact same assets are only redundant duplicates
    of each other if they signal the *same* thing. Different signals (e.g.
    a Provider Placement Opportunity span and a Call Ad Server instant, or
    even two different Start/End pairs) can validly cover identical assets
    at once -- that's multiple distinct SCTE-35 messages at the same
    boundary, not a duplicate."""
    if marker.segmentation is None:
        return (marker.splice_type, None)
    type_id = marker.segmentation.type_id
    value = int(type_id, 16) if isinstance(type_id, str) else int(type_id)
    return (marker.splice_type, f"0x{value:02X}")


class MarkerConfig(SpliceConfig):
    """One node in the flat `markers` list (top-level, sibling of `assets`) --
    the only way to signal ad breaks/placements/etc. A marker names the
    contiguous run of asset `id`s it covers via `assets`, and its start/end
    timestamps are always *derived* from those assets' resolved timeline
    positions (see `timeline.resolve_markers`), never authored directly.
    This is what keeps nested markers frame-accurate by construction: there
    is nothing to desync.

    Nesting (e.g. break ⊃ ppo ⊃ ad) is expressed implicitly by span
    containment across sibling `MarkerConfig` entries, not by an authored
    parent/child relationship -- see `Config.validate_markers` for the
    containment check.
    """

    assets: list[str] = Field(min_length=1)

    @property
    def type(self) -> str:
        """The lane (break/ppo/ad/custom) this marker belongs to, for
        timeline grouping and markers.json labeling -- NOT a stored/authored
        field. Always computed from `segmentation.type_id` (the single
        source of truth for what a marker signals); `splice_insert` markers
        have no type_id to derive from, so this is "ad" (the common case:
        a plain two-point ad splice). There is deliberately no way to set
        this independently and have it disagree with type_id -- that was
        the original bug (a marker could say `type: break` while its
        `segmentation.type_id` was left at an unrelated PPO value)."""
        if self.segmentation is not None:
            return lane_for_type_id(self.segmentation.type_id)
        return "ad"


class AssetConfig(BaseModel):
    file: Path
    id: Optional[str] = None
    start: Optional[TimeValue] = None
    duration: Optional[TimeValue] = None
    fade_in: Optional[TimeValue] = None
    fade_out: Optional[TimeValue] = None
    slate_image: Optional[Path] = None
    # Suppresses the playlist-level OSD entirely for this asset (no bar, no
    # corner text), regardless of the global `Config.osd` settings.
    no_osd: bool = False
    # Free-text label for this asset, shown by the `osd_label` corner
    # content (a corner configured with it shows nothing for assets that
    # leave this unset).
    osd_label: Optional[str] = None

    @field_validator("file", mode="before")
    @classmethod
    def resolve_path(cls, v: object) -> Path:
        return Path(str(v))

    @field_validator("slate_image", mode="before")
    @classmethod
    def resolve_slate_path(cls, v: object) -> Optional[Path]:
        return Path(str(v)) if v is not None else None

    def start_seconds(self) -> Optional[float]:
        if self.start is None:
            return None
        return parse_time(self.start)

    def duration_seconds(self) -> Optional[float]:
        if self.duration is None:
            return None
        return parse_time(self.duration)

    def fade_in_seconds(self) -> Optional[float]:
        if self.fade_in is None:
            return None
        return parse_time(self.fade_in)

    def fade_out_seconds(self) -> Optional[float]:
        if self.fade_out is None:
            return None
        return parse_time(self.fade_out)


# The six things a corner text slot can show. See osd.py's
# build_corner_text_filter for exactly how each resolves to display text.
CornerContent = Literal[
    "asset_id", "time", "next_asset_id", "scte35_spans", "is_adbreak", "osd_label"
]

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def to_ffmpeg_color(hex_color: str) -> str:
    """'#RRGGBB' -> '0xRRGGBB', the format ffmpeg's drawtext fontcolor/
    boxcolor options expect."""
    return "0x" + hex_color.lstrip("#")


class OsdCountdownConfig(BaseModel):
    """A semi-transparent black horizontal bar at the bottom of the frame,
    growing from 0% to 100% width over the current asset's playback."""

    enabled: bool = True
    height_pct: float = Field(default=5.0, ge=0, le=100)


class OsdCornersConfig(BaseModel):
    """Up to 4 independent corner text slots. `None` = nothing shown there."""

    top_left: Optional[CornerContent] = None
    top_right: Optional[CornerContent] = None
    bottom_left: Optional[CornerContent] = "asset_id"
    bottom_right: Optional[CornerContent] = "time"


class OsdCornerBoxConfig(BaseModel):
    """An optional background box drawn behind each corner's text, uniform
    across all 4 corners. The fill itself is a fixed dark gray (not
    configurable -- see osd.py's `_CORNER_BOX_FILL_COLOR`); `color` here
    instead sets a thin vertical border accent flush with the box's outer
    edge (left edge for left corners, right edge for right corners). Sized
    automatically from the rendered text extent plus padding -- see osd.py's
    build_corner_text_filter/build_corner_accent_stripe_graph, which rely
    on ffmpeg drawtext's own `box`/`boxborderw` options rather than
    computing text metrics itself."""

    enabled: bool = False
    color: str = Field(default="#FFFFFF", pattern=_HEX_COLOR_RE.pattern)


class OsdConfig(BaseModel):
    """Playlist-level on-screen display: an optional countdown bar plus up
    to 4 corner text slots, applied to every asset in the playlist except
    those with `AssetConfig.no_osd` set. See `enabled` for the master
    on/off switch -- off by default."""

    enabled: bool = False
    countdown: OsdCountdownConfig = Field(default_factory=OsdCountdownConfig)
    corners: OsdCornersConfig = Field(default_factory=OsdCornersConfig)
    corner_box: OsdCornerBoxConfig = Field(default_factory=OsdCornerBoxConfig)
    # Percentage of the transcoded (rendition) output height, applied
    # uniformly to all 4 corners.
    text_size_pct: float = Field(default=4.0, ge=0, le=100)
    # Applies to all corner text (not the countdown bar, which is always
    # semi-transparent black per spec).
    text_color: str = Field(default="#FFFFFF", pattern=_HEX_COLOR_RE.pattern)
    # Text shown for the `is_adbreak` corner content when true.
    ad_break_label: str = "ad break"

    def ffmpeg_color(self) -> str:
        """'#RRGGBB' -> '0xRRGGBB', the format ffmpeg's drawtext fontcolor
        option expects."""
        return to_ffmpeg_color(self.text_color)


class Config(BaseModel):
    output: OutputConfig
    assets: list[AssetConfig] = Field(min_length=1)
    markers: list[MarkerConfig] = Field(default_factory=list)
    normalize: bool = False
    slate_image: Optional[Path] = None
    osd: OsdConfig = Field(default_factory=OsdConfig)

    @field_validator("slate_image", mode="before")
    @classmethod
    def resolve_global_slate_path(cls, v: object) -> Optional[Path]:
        return Path(str(v)) if v is not None else None

    @model_validator(mode="after")
    def validate_event_ids_unique(self) -> "Config":
        seen: set[int] = set()
        for marker in self.markers:
            eid = marker.event_id
            if eid in seen:
                raise ValueError(f"Duplicate marker event_id: {eid}")
            seen.add(eid)
        return self

    @model_validator(mode="after")
    def validate_asset_ids(self) -> "Config":
        seen: set[str] = set()
        for asset in self.assets:
            if asset.id is None:
                continue
            if asset.id in seen:
                raise ValueError(f"Duplicate asset id: {asset.id!r}")
            seen.add(asset.id)
        return self

    @model_validator(mode="after")
    def validate_markers(self) -> "Config":
        """Validate the flat `markers` list against the asset order:

        - every referenced asset id must exist exactly once in `assets`
        - each marker's own `assets` must be a contiguous run in the
          asset list (no gaps, matching the asset list's own order)
        - every pair of marker spans must be disjoint or one must
          strictly contain the other -- never partially overlapping
          (this is the invariant that stands in for an authored
          parent/child tree; see MarkerConfig docstring)
        """
        if not self.markers:
            return self

        id_to_index: dict[str, int] = {}
        for i, asset in enumerate(self.assets):
            if asset.id is not None:
                id_to_index[asset.id] = i

        spans: list[tuple[int, int, "MarkerConfig"]] = []  # (start_idx, end_idx, marker)
        for marker in self.markers:
            indices: list[int] = []
            for aid in marker.assets:
                if aid not in id_to_index:
                    raise ValueError(
                        f"markers: event_id {marker.event_id} references unknown asset id {aid!r}"
                    )
                indices.append(id_to_index[aid])

            lo, hi = min(indices), max(indices)
            expected = set(range(lo, hi + 1))
            if set(indices) != expected or len(indices) != len(expected):
                raise ValueError(
                    f"markers: event_id {marker.event_id}'s assets {marker.assets!r} "
                    f"are not a contiguous run in the asset list (resolved indices {sorted(indices)})"
                )
            spans.append((lo, hi, marker))

        for i in range(len(spans)):
            lo_a, hi_a, m_a = spans[i]
            for j in range(i + 1, len(spans)):
                lo_b, hi_b, m_b = spans[j]
                disjoint = hi_a < lo_b or hi_b < lo_a
                a_contains_b = lo_a <= lo_b and hi_b <= hi_a
                b_contains_a = lo_b <= lo_a and hi_a <= hi_b
                if disjoint:
                    continue
                if a_contains_b and b_contains_a:
                    # Identical spans are only redundant when both markers
                    # signal the exact same thing (same splice_type and, for
                    # time_signal, the same segmentation.type_id) -- two
                    # different signals (e.g. a Provider Placement
                    # Opportunity span and a Call Ad Server instant, or two
                    # different Start/End pairs) can validly coexist over
                    # identical assets as distinct SCTE-35 messages.
                    if _marker_signal_identity(m_a) == _marker_signal_identity(m_b):
                        raise ValueError(
                            f"markers: event_id {m_a.event_id} and {m_b.event_id} "
                            f"cover the exact same assets -- remove the redundant one"
                        )
                    continue
                if not (a_contains_b or b_contains_a):
                    raise ValueError(
                        f"markers: event_id {m_a.event_id} ({m_a.assets!r}) and "
                        f"event_id {m_b.event_id} ({m_b.assets!r}) partially overlap -- "
                        f"marker spans must be nested or disjoint, never partially overlapping"
                    )
        return self


def load_config(path: str | Path) -> Config:
    """Load and validate a YAML config file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open() as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
