"""Data model (SCOPE.md §5) -- this tool's own normalized representation of
a captured HLS/DASH session's timing, independent of source format.

Every tick value uses TIMESCALE=90_000, matching loop-dee-loop/franken-ts's
own 90kHz clock (loop-dee-loop/SCOPE.md §4.1) -- integers only, never
floats, for the same drift-freedom reasons documented there.
"""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree as ET


@dataclass(frozen=True)
class TimingSegment:
    """One segment on a rendition's own timeline, position/duration only --
    never tied to whether real media bytes were recovered for it (SCOPE.md
    §1's foundational constraint)."""

    index: int
    duration_ticks: int  # at TIMESCALE=90_000, matching loop-dee-loop
    asset_boundary: bool = False  # starts a new asset: HLS discontinuity / DASH new Period
    source_uri: str | None = None  # the manifest-referenced segment URL, for §5.3's
    # archive body lookup; None only if a format's extractor can't recover
    # an absolute URI.


@dataclass(frozen=True)
class AssetSpan:
    """Contiguous run of segments between asset boundaries -- the
    manifest-observable analogue of franken-ts's `assets` list entries
    (franken-ts/AGENTS.md's `markers` section: markers reference "the
    contiguous run of asset ids" they cover). No filename/id available
    from an archive, just the span itself."""

    first_segment_index: int
    segment_count: int
    start_ticks: int
    duration_ticks: int


@dataclass(frozen=True)
class AssetBoundary:
    """A join between two assets, SCOPE.md §6. `gap_ticks` is signed:
    positive = dead time (gap) between the two assets, negative = overlap.
    Always 0 for the very first boundary (index 0 -- there is nothing
    before it to have a gap/overlap against)."""

    segment_index: int  # first segment of the NEW asset
    gap_ticks: int = 0


@dataclass(frozen=True)
class RawMarker:
    """A marker as directly observed in manifest text, before SCTE-35
    binary decode (SCOPE.md §5.2 does that next). `pts_time_ticks` is
    already positioned on this rendition's own timeline (loop-relative,
    same clock as TimingSegment) -- see extract_hls.py/extract_dash.py for
    how each format derives it."""

    source: str  # "daterange" | "cue-out" | "eventstream-bin" | "eventstream-xml"
    pts_time_ticks: int
    splice_command_b64: str | None = None
    # Only for eventstream-xml (mpd-inspector native) -- the raw <Event>
    # (or its SpliceInfoSection child) lxml/ElementTree element, handed
    # to threefive for decode (it accepts XML directly -- see
    # scte35_decode.py).
    splice_command_xml: ET.Element | None = None
    declared_duration_ticks: int | None = None  # manifest-declared, cross-check only
