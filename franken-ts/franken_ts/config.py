from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from scte35_table23 import (
    INSTANT_SEGMENTATION_TYPE_IDS,
    SEGMENTATION_END_TYPE_ID,
    SEGMENTATION_TYPE_CODE,
    SEGMENTATION_TYPE_NAME,
)

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
    segment_num: Optional[int] = Field(default=None, ge=0, le=255)
    segments_expected: Optional[int] = Field(default=None, ge=0, le=255)
    sub_segment_num: Optional[int] = Field(default=None, ge=0, le=255)
    sub_segments_expected: Optional[int] = Field(default=None, ge=0, le=255)

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


# SCTE-35 Table 23 segmentation_type_id (Start value) -> `type` lane label.
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


# SEGMENTATION_TYPE_NAME (bare name, no "Start"/"End" wording) and
# SEGMENTATION_TYPE_CODE (stable three-letter compact code, used by OSD
# spans/reports) now live in the `scte35_table23` package (imported above)
# -- the single source shared with loop-dee-loop's HLS/DASH signaling and
# igor/frontend/src/segmentationPresets.ts, instead of three
# hand-maintained copies. (Not to be confused with inspector-krogh's own
# `scte35_tables.py` -- that's a deliberate, separate duplicate for
# verification independence, not something to unify with this package.)
# Used only for OSD display -- not a source of truth for `type`/lane
# grouping (that's LANE_FOR_SEGMENTATION_TYPE_ID above).


def lane_for_type_id(type_id: str | int) -> str:
    """The `type` lane (break/ppo/ad/custom) a segmentation_type_id belongs
    to -- "custom" for anything not in LANE_FOR_SEGMENTATION_TYPE_ID (e.g.
    Program/Chapter/Credit/Network/standalone-instant types)."""
    value = int(type_id, 16) if isinstance(type_id, str) else int(type_id)
    normalized = f"0x{value:02X}"
    return LANE_FOR_SEGMENTATION_TYPE_ID.get(normalized, "custom")

# SEGMENTATION_END_TYPE_ID (Table 23 Start -> End type_id pairings) and
# INSTANT_SEGMENTATION_TYPE_IDS (signals with no Start/End pairing at all)
# also come from `scte35_table23` -- see the import comment above. The
# Program Early Termination signal remains instant; this editor does not
# expose it as an alternate end for a Program Start or Program Join span.
SEGMENTATION_START_TYPE_IDS = frozenset(SEGMENTATION_END_TYPE_ID)
SEGMENTATION_END_TYPE_IDS = frozenset(SEGMENTATION_END_TYPE_ID.values())


def segmentation_end_type_id(type_id: int) -> int:
    """Return the Table 23 End type for a paired segmentation Start."""
    return SEGMENTATION_END_TYPE_ID.get(type_id, type_id + 1)


def is_segmentation_start_type_id(type_id: int) -> bool:
    """Whether a raw Table 23 ID represents a start or standalone signal."""
    normalized = f"0x{type_id:02X}"
    if normalized in INSTANT_SEGMENTATION_TYPE_IDS:
        return True
    if type_id in SEGMENTATION_START_TYPE_IDS:
        return True
    if type_id in SEGMENTATION_END_TYPE_IDS:
        return type_id in SEGMENTATION_START_TYPE_IDS
    return type_id % 2 == 0


def is_instant_segmentation(segmentation: Optional["SegmentationConfig"]) -> bool:
    """True if `segmentation` describes a standalone/instant signal (see
    INSTANT_SEGMENTATION_TYPE_IDS) rather than a Start/End pair."""
    if segmentation is None:
        return False
    type_id = segmentation.type_id
    value = int(type_id, 16) if isinstance(type_id, str) else int(type_id)
    normalized = f"0x{value:02X}"
    return normalized in INSTANT_SEGMENTATION_TYPE_IDS


def abbreviation_for_marker(marker: "MarkerConfig") -> str:
    """Abbreviation shown for one covering SCTE-35 span (OSD `scte35_spans`
    corner and the loop progress bar): 'SPI' for a bare splice_insert (no
    segmentation descriptor); otherwise the stable three-letter code from
    SEGMENTATION_TYPE_CODE, falling back to the raw type_id string."""
    if marker.segmentation is None:
        return "SPI"

    type_id = marker.segmentation.type_id
    value = int(type_id, 16) if isinstance(type_id, str) else int(type_id)
    normalized = f"0x{value:02X}"
    return SEGMENTATION_TYPE_CODE.get(normalized, normalized)


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


def _semantic_segment_role(marker: "MarkerConfig") -> tuple[str, int] | None:
    """Map a marker to its SCTE-35 segment family and hierarchy level.

    Levels increase from outer to inner. Instant descriptors, splice_insert,
    and Table 23 types outside the selected Content/Advertising/Alternate
    hierarchies are intentionally ignored by span hierarchy validation.
    """
    if marker.splice_type != "time_signal" or marker.segmentation is None:
        return None
    value = int(marker.segmentation.type_id, 16)
    if value in INSTANT_SEGMENTATION_TYPE_IDS:
        return None
    content_levels = {0x50: 0, 0x10: 1, 0x17: 1, 0x19: 1, 0x20: 2}
    advertising_levels = {
        0x22: 0,
        0x34: 1, 0x36: 1, 0x38: 1, 0x3A: 1,
        0x44: 2, 0x46: 2,
        0x30: 3, 0x32: 3, 0x3C: 3, 0x3E: 3,
    }
    if value in content_levels:
        return ("content", content_levels[value])
    if value in advertising_levels:
        return ("advertising", advertising_levels[value])
    if value == 0x42:
        return ("alternate", 0)
    return None


def _populate_and_validate_segment_numbering(
    spans: list[tuple[int, int, "MarkerConfig"]],
    scheme: str,
    break_numbering_supported: bool,
    assets: list["AssetConfig"],
) -> None:
    """Resolve the profile's two independent counting tiers from asset spans.

    Equal spans can represent distinct hierarchy levels, so containment here
    includes equality. Ties are ordered by type level and then event ID.
    """
    typed = [(lo, hi, m, int(m.segmentation.type_id, 16)) for lo, hi, m in spans
             if m.splice_type == "time_signal" and m.segmentation is not None]
    programs = [s for s in typed if s[3] in {0x10, 0x17, 0x19}]
    breaks = [s for s in typed if s[3] == 0x22]
    po_types = {0x34, 0x36, 0x38, 0x3A}
    block_types = {0x44, 0x46}
    ad_types = {0x30, 0x32, 0x3C, 0x3E}
    asset_roles = {asset.id: asset.role for asset in assets if asset.id is not None}

    def asset_role(item):
        # A marker can cover multiple contiguous clips (e.g. a split video),
        # but all clips in the same PAD must have the same classification.
        roles = {asset_roles[asset_id] == "jingle" for asset_id in item[2].assets}
        if len(roles) != 1:
            raise ValueError(f"markers: AF2M_SNPTV Provider Advertisement {item[2].event_id} covers assets with conflicting roles")
        return roles.pop()

    def parent(item, candidates):
        containing = [p for p in candidates if p[2] is not item[2]
                      and p[0] <= item[0] and item[1] <= p[1]]
        return min(containing, key=lambda p: (p[1] - p[0], p[0], p[2].event_id)) if containing else None

    def ordered(items):
        return sorted(items, key=lambda s: (s[0], s[1], s[2].event_id))

    def values(item, outer=(0, 0), inner=None):
        seg = item[2].segmentation
        assert seg is not None
        if max(outer) > 255 or (inner is not None and max(inner) > 255):
            raise ValueError("markers: numbering exceeds the SCTE-35 8-bit field range")
        seg.segment_num, seg.segments_expected = outer
        seg.sub_segment_num, seg.sub_segments_expected = inner if inner is not None else (None, None)

    intervals = [s[2].break_interval for s in ordered(breaks) if parent(s, programs) is None]
    if break_numbering_supported and scheme != "AF2M_SNPTV" and intervals != sorted(intervals):
        raise ValueError("markers: break_interval must increase in playback order when numbering Breaks without a Program")

    for item in typed:
        t = item[3]
        seg = item[2].segmentation
        assert seg is not None
        if (seg.sub_segment_num is not None or seg.sub_segments_expected is not None) and (
            scheme == "AF2M_SNPTV"
            or (scheme == "SCTE35_2019A" and t not in po_types)
            or (scheme == "SCTE35_2023R1" and t not in po_types | block_types | ad_types)
        ):
            raise ValueError(f"markers: event_id {item[2].event_id} cannot specify sub_segment_* in {scheme}")
        if scheme == "AF2M_SNPTV" and t not in {0x22, 0x34, 0x30, 0x02}:
            raise ValueError(f"markers: event_id {item[2].event_id} uses a type unsupported by AF2M_SNPTV")
        if scheme == "SCTE35_2019A" and t in block_types:
            raise ValueError(f"markers: event_id {item[2].event_id} uses Ad Block, absent from SCTE35_2019A")
        if item[2].break_interval != 1 and t != 0x22:
            raise ValueError("markers: break_interval is only valid on Break markers")
        if t in {0x10, 0x13, 0x14, 0x15, 0x16, 0x17, 0x19}:
            values(item, (1, 1))
        elif t in {0x24, 0x26}:
            values(item, (1, 1))
        else:
            values(item)

    for chapter in ordered([s for s in typed if s[3] == 0x20]):
        chapter_parent = parent(chapter, programs)
        siblings = ordered([s for s in typed if s[3] == 0x20 and parent(s, programs) is chapter_parent])
        values(chapter, (next(i for i, s in enumerate(siblings, 1) if s[2] is chapter[2]), len(siblings)))

    break_numbers = {}
    for br in ordered(breaks):
        program = parent(br, programs)
        if scheme == "AF2M_SNPTV":
            number = (1, 1)
        elif break_numbering_supported:
            siblings = ordered([s for s in breaks if parent(s, programs) is program
                                and (program is not None or s[2].break_interval == br[2].break_interval)])
            number = (next(i for i, s in enumerate(siblings, 1) if s[2] is br[2]), len(siblings))
        else:
            number = (0, 0)
        values(br, number)
        break_numbers[id(br[2])] = number

    for item in ordered([s for s in typed if s[3] in po_types | block_types | ad_types]):
        br = parent(item, breaks)
        t = item[3]
        if scheme == "AF2M_SNPTV" and br is None:
            raise ValueError(f"markers: AF2M_SNPTV event_id {item[2].event_id} needs a containing Break")
        collection = [s for s in typed if parent(s, breaks) is br]
        outer = break_numbers[id(br[2])] if br is not None else (0, 0)
        if scheme == "AF2M_SNPTV":
            if t in po_types:
                values(item, (1, 1))
            else:
                spots = ordered([s for s in collection if s[3] == 0x30 and not asset_role(s)])
                if not asset_role(item):
                    pos = next(i for i, s in enumerate(spots, 1) if s[2] is item[2])
                    values(item, (pos, len(spots)))
                else:
                    ads_in_break = ordered([s for s in collection if s[3] == 0x30])
                    if ads_in_break[0][2] is item[2] and item[0] == br[0]:
                        values(item, (0, len(spots)))
                    elif ads_in_break[-1][2] is item[2] and item[1] == br[1]:
                        values(item, (0, 0))
                    else:
                        raise ValueError(f"markers: AF2M_SNPTV jingle {item[2].event_id} must be at the start or end of its Break")
        elif scheme == "SCTE35_2019A":
            if t in po_types:
                pos = ordered([s for s in collection if s[3] in po_types])
                inner = (next(i for i, s in enumerate(pos, 1) if s[2] is item[2]), len(pos)) if br else None
                values(item, outer, inner)
            else:
                ads = ordered([s for s in collection if s[3] in ad_types])
                values(item, (next(i for i, s in enumerate(ads, 1) if s[2] is item[2]), len(ads)) if br else (0, 0))
        else:
            if t in po_types:
                group = ordered([s for s in collection if s[3] in po_types])
                inner = (next(i for i, s in enumerate(group, 1) if s[2] is item[2]), len(group)) if br else None
            elif t in ad_types:
                group = ordered([s for s in collection if s[3] in ad_types])
                inner = (next(i for i, s in enumerate(group, 1) if s[2] is item[2]), len(group)) if br else None
            else:
                ads = ordered([s for s in collection if s[3] in ad_types])
                blocks = [s for s in collection if s[3] in block_types]
                first = next((i for i, ad in enumerate(ads, 1) if item[0] <= ad[0] and ad[1] <= item[1]), None)
                if first is None:
                    raise ValueError(f"markers: Ad Block {item[2].event_id} needs an underlying Advertisement or Promo")
                inner = (first, len(blocks))
            values(item, outer, inner)


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
    break_interval: int = Field(default=1, ge=1)

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
    # Content role is independent of any signaling scheme. Only af2m currently
    # uses Jingle when numbering Provider Advertisement descriptors.
    role: Optional[Literal["advert", "jingle"]] = None
    start: Optional[TimeValue] = None
    duration: Optional[TimeValue] = None
    fade_in: Optional[TimeValue] = None
    fade_out: Optional[TimeValue] = None
    slate_image: Optional[Path] = None
    # Extra HTTP request headers (e.g. Referer, a bearer token) needed to
    # fetch a remote `file` -- most commonly a CDN-hosted HLS/DASH manifest
    # that Referer/User-Agent-checks unauthenticated requests (see
    # stream_source.py). Ignored for local files.
    headers: Optional[dict[str, str]] = None
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


# The corner text slot content types. See osd.py's
# build_corner_text_filter for exactly how each resolves to display text.
CornerContent = Literal[
    "asset_id", "time", "loop_time", "transition", "next_asset_id", "scte35_spans", "is_adbreak", "osd_label"
]

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def to_ffmpeg_color(hex_color: str) -> str:
    """'#RRGGBB' -> '0xRRGGBB', the format ffmpeg's drawtext fontcolor/
    boxcolor options expect."""
    return "0x" + hex_color.lstrip("#")


class OsdProgressBarConfig(BaseModel):
    """Where the playback is shown along the bottom of the frame. `asset`: a
    semi-transparent black bar growing 0->100% over the current asset (resets
    per asset). `loop`: a static two-row map of the whole loop (one row of
    assets, one of SCTE-35 spans) with a playhead. `none`: nothing.
    `height_pct` is the bar's height in `asset` mode and the height of EACH
    of the two rows in `loop` mode."""

    mode: Literal["asset", "loop", "none"] = "asset"
    height_pct: float = Field(default=3.0, ge=0, le=50)


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
    color: str = Field(default="#000000", pattern=_HEX_COLOR_RE.pattern)


class OsdConfig(BaseModel):
    """Playlist-level on-screen display: an optional progress bar plus up
    to 4 corner text slots, applied to every asset in the playlist except
    those with `AssetConfig.no_osd` set. See `enabled` for the master
    on/off switch -- off by default."""

    enabled: bool = False
    progress_bar: OsdProgressBarConfig = Field(default_factory=OsdProgressBarConfig)
    corners: OsdCornersConfig = Field(default_factory=OsdCornersConfig)
    corner_box: OsdCornerBoxConfig = Field(default_factory=OsdCornerBoxConfig)
    # Percentage of the transcoded (rendition) output height, applied
    # uniformly to all 4 corners.
    text_size_pct: float = Field(default=3.0, ge=0, le=100)
    # Applies to all corner text (not the progress bar, which is always
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
    enforce_scte35_marker_semantics: bool = True
    scte35_numbering_scheme: Literal["SCTE35_2019A", "SCTE35_2023R1", "AF2M_SNPTV"] = "SCTE35_2023R1"
    break_numbering_supported: bool = False
    normalize: bool = False
    slate_image: Optional[Path] = None
    osd: OsdConfig = Field(default_factory=OsdConfig)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_jingle_roles(cls, raw: object) -> object:
        """Read previous marker- and asset-level jingle_role spellings."""
        if not isinstance(raw, dict):
            return raw
        assets = [dict(asset) for asset in raw.get("assets", [])]
        for asset in assets:
            old_role = asset.pop("jingle_role", None)
            if old_role is not None:
                new_role = "jingle" if old_role in ("opening", "closing") else None
                if asset.get("role", new_role) != new_role:
                    raise ValueError(f"assets: conflicting role and legacy jingle_role on {asset.get('id')!r}")
                if new_role is not None:
                    asset["role"] = new_role
        by_id = {asset.get("id"): asset for asset in assets}
        markers = []
        for original in raw.get("markers", []):
            marker = dict(original)
            role = marker.pop("jingle_role", None)
            if role in ("opening", "closing"):
                for asset_id in marker.get("assets", []):
                    if asset_id not in by_id:
                        raise ValueError(f"markers: legacy jingle_role references unknown asset {asset_id!r}")
                    asset = by_id[asset_id]
                    if asset.get("role", "jingle") != "jingle":
                        raise ValueError(f"markers: conflicting role on asset {asset_id!r}")
                    asset["role"] = "jingle"
            markers.append(marker)
        return {**raw, "assets": assets, "markers": markers}

    @field_validator("slate_image", mode="before")
    @classmethod
    def resolve_global_slate_path(cls, v: object) -> Optional[Path]:
        return Path(str(v)) if v is not None else None

    @model_validator(mode="after")
    def validate_event_ids_unique(self) -> "Config":
        if not self.enforce_scte35_marker_semantics:
            return self
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
        - spans must be contiguous regardless of semantic enforcement
        - when SCTE semantics are enforced, marker relationships must
          follow the Table 23 hierarchy
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

        if not self.enforce_scte35_marker_semantics:
            return self

        for marker in self.markers:
            if marker.splice_type != "time_signal" or marker.segmentation is None:
                continue
            type_id = int(marker.segmentation.type_id, 16)
            if type_id in SEGMENTATION_END_TYPE_IDS and type_id not in SEGMENTATION_START_TYPE_IDS:
                raise ValueError(
                    f"markers: event_id {marker.event_id} uses segmentation End type "
                    f"0x{type_id:02X}; select its Start type instead"
                )

        for i in range(len(spans)):
            lo_a, hi_a, m_a = spans[i]
            for j in range(i + 1, len(spans)):
                lo_b, hi_b, m_b = spans[j]
                disjoint = hi_a < lo_b or hi_b < lo_a
                if disjoint:
                    continue

                if (lo_a, hi_a) == (lo_b, hi_b) and _marker_signal_identity(m_a) == _marker_signal_identity(m_b):
                    raise ValueError(
                        f"markers: event_id {m_a.event_id} and {m_b.event_id} "
                        "cover the exact same assets with the same signal"
                    )

                relation_a = _semantic_segment_role(m_a)
                relation_b = _semantic_segment_role(m_b)
                # Non-segmenting/unknown descriptors don't participate in
                # the hierarchy or span overlap policy.
                if relation_a is None or relation_b is None:
                    continue

                if m_a.splice_type != "time_signal" or m_b.splice_type != "time_signal":
                    continue

                a_contains_b = lo_a <= lo_b and hi_b <= hi_a
                b_contains_a = lo_b <= lo_a and hi_a <= hi_b
                same_category = relation_a[0] == relation_b[0]

                # Chapters explicitly may overlap. Placement Opportunities
                # may nest, but may not partially cross one another.
                if relation_a == ("content", 2) and relation_b == ("content", 2):
                    continue
                # Program Overlap Start explicitly permits an embedded
                # Program to begin before the active Program ends.
                program_overlap_pair = (
                    relation_a == ("content", 1)
                    and relation_b == ("content", 1)
                    and (int(m_a.segmentation.type_id, 16) == 0x17
                         or int(m_b.segmentation.type_id, 16) == 0x17)
                )
                if program_overlap_pair:
                    continue
                if relation_a[0] == "alternate" and relation_b[0] == "alternate":
                    if a_contains_b or b_contains_a:
                        continue
                    raise ValueError(
                        f"markers: Alternate Content Opportunities {m_a.event_id} and "
                        f"{m_b.event_id} must be nested or disjoint"
                    )
                if relation_a == ("advertising", 1) and relation_b == ("advertising", 1):
                    if a_contains_b or b_contains_a:
                        continue
                    raise ValueError(
                        f"markers: Placement Opportunities {m_a.event_id} and {m_b.event_id} "
                        "partially overlap; they must be nested or disjoint"
                    )

                if same_category:
                    category_a, level_a = relation_a
                    _, level_b = relation_b
                    if level_a == level_b:
                        # Ads and Promos share the lowest logical level and
                        # cannot overlap; similarly forbid same-level Network,
                        # Program, Break, Ad Block, and Promo/Ad spans.
                        raise ValueError(
                            f"markers: {m_a.event_id} and {m_b.event_id} overlap "
                            f"at the same {category_a} segmentation level"
                        )
                    outer_is_a = level_a < level_b
                    valid = a_contains_b if outer_is_a else b_contains_a
                    if not valid:
                        raise ValueError(
                            f"markers: {m_a.event_id} and {m_b.event_id} violate "
                            "the Network/Program/Chapter or Break/Placement Opportunity/"
                            "Ad Block/Advertisement hierarchy"
                        )
                    continue

                if relation_a == ("alternate", 0) and relation_b[0] in {"content", "advertising"}:
                    if not b_contains_a:
                        raise ValueError(f"markers: Alternate Content Opportunity {m_a.event_id} must be within Content or Advertising")
                    continue
                if relation_b == ("alternate", 0) and relation_a[0] in {"content", "advertising"}:
                    if not a_contains_b:
                        raise ValueError(f"markers: Alternate Content Opportunity {m_b.event_id} must be within Content or Advertising")
                    continue

                # Alternate Content Opportunities can only be nested within
                # content or advertising. Advertising may occur inside a
                # Program/Chapter or between them, but must not cross their
                # boundaries.
                if relation_a[0] == "alternate" or relation_b[0] == "alternate":
                    alternate_is_a = relation_a[0] == "alternate"
                    valid = b_contains_a if alternate_is_a else a_contains_b
                else:
                    content_is_a = relation_a[0] == "content"
                    valid = a_contains_b if content_is_a else b_contains_a
                if not valid:
                    raise ValueError(
                        f"markers: {m_a.event_id} and {m_b.event_id} overlap without "
                        "a valid containing relationship"
                    )

        _populate_and_validate_segment_numbering(spans, self.scte35_numbering_scheme, self.break_numbering_supported, self.assets)
        return self


def load_config(path: str | Path) -> Config:
    """Load and validate a YAML config file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open() as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
