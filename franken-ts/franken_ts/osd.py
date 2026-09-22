from __future__ import annotations

"""
On-screen display (OSD) overlay filter construction.

Builds the ffmpeg `drawbox`/`drawtext` filter strings for the playlist-level
OSD: an optional countdown progress bar plus up to 4 corner text slots (see
`config.OsdConfig`). Kept separate from extract.py -- which is exclusively
concerned with frame-accurate extraction/timestamp hazards (see that
module's docstring) -- since this is unrelated, purely presentational logic
with enough surface area (bar + 4 independently-gated text slots + escaping
+ abbreviation derivation) to warrant its own pure-function unit tests.
"""

from typing import Literal, Optional

from .config import MarkerConfig, OsdConfig, OutputConfig, SEGMENTATION_TYPE_NAME, to_ffmpeg_color
from .timeline import TimelineEntry

Corner = Literal["top_left", "top_right", "bottom_left", "bottom_right"]

# Margin from the frame edge, as a fraction of output height. No spacing
# value was specified by the feature's spec -- this is a modest, legible
# default.
_MARGIN_FRACTION = 0.015

# Opacity of the optional corner background box (OsdCornerBoxConfig) -- not
# user-configurable, just the color is; "semi-transparent" per spec.
_CORNER_BOX_ALPHA = 0.6

# Corner box padding, as a fraction of fontsize -- ffmpeg's drawtext `box`
# option sizes the box automatically from the rendered text extent (tw/th),
# so this only needs to supply the "nice padding" around that text, not any
# text-measurement of our own.
_CORNER_BOX_PADDING_FRACTION = 0.3


def escape_ffmpeg_text(text: str) -> str:
    """Escape a literal string for use inside a `drawtext=text='...'` value.

    Generalizes the inline escaping the old countdown overlay duplicated ad
    hoc. Apply this ONLY to literal/dynamic display strings (asset ids,
    "ad break", joined abbreviations) -- NOT to hand-built `%{eif\\:...}`
    expression templates, which pre-escape their own structural colons/
    commas and would be double-escaped if run through this.
    """
    return text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")


def abbreviation_for_marker(marker: MarkerConfig) -> str:
    """Abbreviation shown for one covering SCTE-35 span in the
    `scte35_spans` corner content: 'splice' for a bare splice_insert
    (no segmentation descriptor); otherwise lowercase initials of the
    segmentation type's bare name (config.SEGMENTATION_TYPE_NAME), e.g.
    'Break' -> 'b', 'Provider Placement Opportunity' -> 'ppo',
    'Provider Advertisement' -> 'pa'. Falls back to the raw type_id string
    if it isn't in the table.
    """
    if marker.segmentation is None:
        return "splice"

    type_id = marker.segmentation.type_id
    value = int(type_id, 16) if isinstance(type_id, str) else int(type_id)
    normalized = f"0x{value:02X}"
    name = SEGMENTATION_TYPE_NAME.get(normalized)
    if name is None:
        return normalized.lower()
    return "".join(word[0] for word in name.split() if word[0].isalpha()).lower()


def build_progress_bar_filter(clip_dur: float, output: OutputConfig, height_pct: float) -> str:
    """A semi-transparent black horizontal bar at the bottom of the frame,
    growing from 0% to 100% width over the clip's playback.

    height_pct is a percentage of the *output* (rendition) height, so the
    bar scales correctly across a multi-rendition ABR ladder. The internal
    comma in the `w=` expression must be backslash-escaped -- same
    precedent as the old countdown digit's `max(0\\,(...))` -- since an
    unescaped comma ends the current filter in ffmpeg's filtergraph syntax.
    """
    h = round(output.height * height_pct / 100)
    y = output.height - h
    w_expr = f"iw*min(1\\,t/{clip_dur:.6f})"
    return f"drawbox=x=0:y={y}:w='{w_expr}':h={h}:color=0x000000@0.5:t=fill"


def _corner_content_text(
    content: str,
    entry: TimelineEntry,
    clip_dur: float,
    osd: OsdConfig,
) -> Optional[str]:
    """Resolve one corner's configured content type to a drawtext `text=`
    value (already escaped where needed), or None if there's nothing to
    show for this entry."""
    if content == "asset_id":
        if entry.asset_id is None:
            return None
        return escape_ffmpeg_text(entry.asset_id)

    if content == "time":
        # Counts UP (elapsed seconds since the asset's own start), format
        # "elapsed/total" with 2 decimal places, e.g. "12.32/34.60" -- a
        # live-ticking ffmpeg expression, not escaped via escape_ffmpeg_text
        # since it's a structural template. ffmpeg's `eif` only formats
        # integers, so the fractional part is computed separately (as
        # centiseconds, 0-99, zero-padded to 2 digits via eif's width arg)
        # and joined with a literal '.'; `total` is static per-clip, so it's
        # just formatted directly in Python.
        total = f"{clip_dur:.2f}"
        whole = "%{eif\\:trunc(t)\\:d}"
        centis = "%{eif\\:trunc(mod(t\\,1)*100)\\:d\\:2}"
        return f"{whole}.{centis}/{total}"

    if content == "next_asset_id":
        # Always set -- playlists loop, see timeline.py's wraparound scan --
        # except in the harmless edge case where every other asset is a
        # still image.
        if entry.next_asset_id is None:
            return None
        return escape_ffmpeg_text(f"next: {entry.next_asset_id}")

    if content == "scte35_spans":
        if not entry.covering_spans:
            return None
        abbrevs = " / ".join(abbreviation_for_marker(m) for m in entry.covering_spans)
        return escape_ffmpeg_text(abbrevs)

    if content == "is_adbreak":
        if not entry.is_adbreak:
            return None
        return escape_ffmpeg_text(osd.ad_break_label)

    if content == "osd_label":
        if not entry.osd_label:
            return None
        return escape_ffmpeg_text(entry.osd_label)

    return None


def build_corner_text_filter(
    content: str,
    entry: TimelineEntry,
    output: OutputConfig,
    osd: OsdConfig,
    corner: Corner,
    clip_dur: float,
) -> Optional[str]:
    """Return a `drawtext` filter string for one corner, or None if this
    entry has nothing to show for the configured content type."""
    text = _corner_content_text(content, entry, clip_dur, osd)
    if text is None:
        return None

    fontsize = round(output.height * osd.text_size_pct / 100)
    margin = round(output.height * _MARGIN_FRACTION)

    bar_h = round(output.height * osd.countdown.height_pct / 100) if osd.countdown.enabled else 0
    bottom_offset = margin + bar_h + (margin if bar_h else 0)

    x = f"{margin}" if "left" in corner else f"w-tw-{margin}"
    y = f"{margin}" if "top" in corner else f"h-th-{bottom_offset}"

    box_part = ""
    if osd.corner_box.enabled:
        padding = round(fontsize * _CORNER_BOX_PADDING_FRACTION)
        box_color = to_ffmpeg_color(osd.corner_box.color)
        box_part = f":box=1:boxcolor={box_color}@{_CORNER_BOX_ALPHA}:boxborderw={padding}"

    return (
        f"drawtext=text='{text}':fontsize={fontsize}:fontcolor={osd.ffmpeg_color()}:"
        f"x={x}:y={y}{box_part}"
    )


def build_osd_filters(
    entry: TimelineEntry,
    output: OutputConfig,
    osd: Optional[OsdConfig],
) -> list[str]:
    """Top-level orchestrator: returns the ordered list of ffmpeg filter
    strings for this entry's OSD, or [] when there's nothing to draw
    (OSD unset/disabled globally, or suppressed for this asset via
    `no_osd`). Draw order: bar first, then corners in a fixed order."""
    if osd is None or not osd.enabled or entry.no_osd:
        return []

    clip_dur = entry.clip_duration
    filters: list[str] = []

    if osd.countdown.enabled:
        filters.append(build_progress_bar_filter(clip_dur, output, osd.countdown.height_pct))

    corners: list[tuple[Corner, Optional[str]]] = [
        ("top_left", osd.corners.top_left),
        ("top_right", osd.corners.top_right),
        ("bottom_left", osd.corners.bottom_left),
        ("bottom_right", osd.corners.bottom_right),
    ]
    for corner, content in corners:
        if content is None:
            continue
        f = build_corner_text_filter(content, entry, output, osd, corner, clip_dur)
        if f is not None:
            filters.append(f)

    return filters
