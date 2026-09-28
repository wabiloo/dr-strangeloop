from __future__ import annotations

"""
On-screen display (OSD) overlay filter construction.

Builds the ffmpeg filtergraph fragments for the playlist-level OSD: an
optional countdown progress bar plus up to 4 corner text slots (see
`config.OsdConfig`). Kept separate from extract.py -- which is exclusively
concerned with frame-accurate extraction/timestamp hazards (see that
module's docstring) -- since this is unrelated, purely presentational logic
with enough surface area (bar + 4 independently-gated text slots + escaping
+ abbreviation derivation) to warrant its own pure-function unit tests.
"""

from typing import Literal, Optional

from .config import (
    MarkerConfig,
    OsdConfig,
    OutputConfig,
    SEGMENTATION_TYPE_CODE,
    to_ffmpeg_color,
)
from .timeline import TimelineEntry

Corner = Literal["top_left", "top_right", "bottom_left", "bottom_right"]

# Margin from the frame edge, as a fraction of output height. No spacing
# value was specified by the feature's spec -- this is a modest, legible
# default.
_MARGIN_FRACTION = 0.03

# Opacity of the corner background box's fill -- genuinely semi-transparent,
# by design (so video content stays partially visible through the box).
_CORNER_BOX_ALPHA = 0.6

# Fixed fill color for the optional corner box -- NOT user-configurable.
# OsdCornerBoxConfig.color now controls only the border accent stripe (see
# build_corner_accent_stripe_graph); a neutral dark gray fill reads cleanly
# against any video content and against any border color, avoiding the
# earlier design where a single user-picked color had to double as both a
# fill AND a legible accent, which pulled it in two directions at once.
_CORNER_BOX_FILL_COLOR = to_ffmpeg_color("#262626")

# Corner box padding, as a fraction of fontsize -- ffmpeg's drawtext `box`
# option sizes the box automatically from the rendered text extent (tw/th),
# so this only needs to supply the "nice padding" around that text, not any
# text-measurement of our own.
_CORNER_BOX_PADDING_FRACTION = 0.3

# Opacity of the optional corner-box border accent stripe -- fully opaque.
# An earlier version used a semi-transparent stripe (0.9) that relied on
# landing pixel-perfect flush against the fill box's edge; ffmpeg's crop
# rounding (see build_corner_accent_stripe_graph's docstring) could shave a
# pixel or two off that edge, and because the stripe was translucent, the
# gap showed through as a visible sliver of untouched background rather
# than being masked by the stripe's own color. Fully opaque plus a deliberate
# _ACCENT_STRIPE_OVERLAP_PX (below) means even if the two edges don't meet
# exactly, the stripe unambiguously covers the seam instead of blending with
# whatever's behind it.
_ACCENT_STRIPE_ALPHA = 1.0

# Accent stripe thickness, as a fraction of fontsize.
_ACCENT_STRIPE_WIDTH_FRACTION = 0.22

# Deliberate overlap (in pixels) between the accent stripe and the corner
# fill box it sits against, biased inward (toward the box) from the stripe's
# nominal flush position. Guarantees the stripe always paints over part of
# the box's own edge rather than merely abutting it -- so any sub-pixel/
# rounding mismatch between the two independently-positioned elements is
# hidden under the (now fully opaque) stripe instead of opening a gap.
_ACCENT_STRIPE_OVERLAP_PX = 2

# How long the Transition corner countdown is shown before an asset boundary.
_TRANSITION_COUNTDOWN_SECONDS = 5.0


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
    `scte35_spans` corner content: 'SPI' for a bare splice_insert
    (no segmentation descriptor); otherwise the stable three-letter code
    from config.SEGMENTATION_TYPE_CODE. Falls back to the raw type_id string
    if it isn't in the table.
    """
    if marker.segmentation is None:
        return "SPI"

    type_id = marker.segmentation.type_id
    value = int(type_id, 16) if isinstance(type_id, str) else int(type_id)
    normalized = f"0x{value:02X}"
    return SEGMENTATION_TYPE_CODE.get(normalized, normalized)


def build_progress_bar_graph(
    input_label: str,
    output_label: str,
    clip_dur: float,
    output: OutputConfig,
    height_pct: float,
    label_prefix: str,
) -> list[str]:
    """A semi-transparent black horizontal bar at the bottom of the frame,
    growing from 0% to 100% width over the clip's playback -- returns a list
    of `;`-joinable filtergraph statements from `input_label` to
    `[{output_label}]`, using `label_prefix` for its own internal pad names
    (caller picks a prefix that can't collide with any other label already
    used in the same filter_complex graph).

    This is NOT a single `drawbox` filter. drawbox's x/y/w/h expressions
    have no time-aware constant at all -- per ffmpeg-filters(1), its ONLY
    single-letter constant is `t`, which is an alias for the THICKNESS
    option, not "current time" (unlike drawtext, crop, overlay, etc). An
    earlier version of this used `w='iw*min(1,t/clip_dur)'` expecting `t` to
    mean time; instead it silently picked up the internal FILL sentinel for
    thickness=fill (a large constant), so `min(1, huge)` was always 1 and
    the bar rendered permanently full-width from frame 0 -- confirmed by
    direct pixel sampling of ffmpeg's own output, not just reading docs.

    Genuine per-frame animation instead follows the same technique already
    proven in production at ../opinionated-streaming-media-processor's
    apply_progressbar(): render a full-frame-width, bar-height rectangle via
    the `color` source, give it a real alpha channel via `format=rgba,
    colorchannelmixer=aa=<alpha>` (the `color` source's own `@alpha` suffix
    does not reliably propagate into `overlay`'s blending -- that repo
    switched to colorchannelmixer for the same reason), then slide it into
    view by animating `overlay`'s own `x` from `-w` (fully off-canvas, 0%
    visible) to `0` (fully on-canvas, 100% visible) over the clip's
    duration. Unlike drawbox, `overlay`'s x/y ARE genuinely time-aware and
    evaluated per-frame by default (`eval=frame`, confirmed in
    ffmpeg-filters(1) -- no extra flag needed).
    """
    h = round(output.height * height_pct / 100)
    y = output.height - h
    bar = f"{label_prefix}bar"
    x_expr = f"w*(min(1\\,t/{clip_dur:.6f})-1)"
    return [
        f"color=c=black:s={output.width}x{h}:d={clip_dur:.6f}:r={output.framerate},"
        f"format=rgba,colorchannelmixer=aa=0.5 [{bar}]",
        f"{input_label} [{bar}] overlay=x='{x_expr}':y={y} [{output_label}]",
    ]


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

    if content == "loop_time":
        # Each asset is extracted as an independent clip, so ffmpeg's `t`
        # restarts at zero for every asset. Add the clip's playlist offset
        # to show one continuous elapsed/total clock for the whole loop.
        # Keep the current value live-ticking and centisecond-formatted just
        # like the asset-level `time` option above.
        start = f"{entry.output_start:.6f}"
        total = f"{entry.loop_duration:.2f}"
        elapsed = f"(t+{start})"
        whole = f"%{{eif\\:trunc({elapsed})\\:d}}"
        centis = f"%{{eif\\:trunc(mod({elapsed}\\,1)*100)\\:d\\:2}}"
        return f"{whole}.{centis}/{total}"

    if content == "transition":
        # Show a 5-second countdown immediately before every asset cut. The
        # final asset's outgoing cut is the playlist loop boundary, not a
        # regular asset transition. Clips shorter than 3 seconds count down
        # from their own duration instead of displaying negative values.
        if abs(entry.output_end - entry.loop_duration) < 1e-6:
            label = "Loop End"
        else:
            label = entry.role or "Asset"
        countdown_start = max(0.0, clip_dur - _TRANSITION_COUNTDOWN_SECONDS)
        countdown_expression = f"max(0\\,{clip_dur:.6f}-t)"
        whole = f"%{{eif\\:trunc({countdown_expression})\\:d}}"
        centis = f"%{{eif\\:trunc(mod({countdown_expression}\\,1)*100)\\:d\\:2}}"
        return f"{escape_ffmpeg_text(label)} in {whole}.{centis}"

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


def _corner_geometry(output: OutputConfig, osd: OsdConfig) -> tuple[int, int, int]:
    """Shared sizing for a corner's text and (if enabled) its accent stripe:
    `(fontsize, margin, bottom_offset)`. `bottom_offset` is the distance
    from the frame's bottom edge to the bottom of bottom-corner text -- with
    the countdown bar enabled, that's the bar's height plus one `margin`-
    sized gap above it, deliberately the SAME gap as a top corner's distance
    from the top edge (not, say, double it) so top and bottom spacing read
    as consistent.
    """
    fontsize = round(output.height * osd.text_size_pct / 100)
    margin = round(output.height * _MARGIN_FRACTION)
    bar_h = round(output.height * osd.countdown.height_pct / 100) if osd.countdown.enabled else 0
    bottom_offset = margin + bar_h
    return fontsize, margin, bottom_offset


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

    fontsize, margin, bottom_offset = _corner_geometry(output, osd)

    x = f"{margin}" if "left" in corner else f"w-tw-{margin}"
    y = f"{margin}" if "top" in corner else f"h-th-{bottom_offset}"

    box_part = ""
    if osd.corner_box.enabled:
        padding = round(fontsize * _CORNER_BOX_PADDING_FRACTION)
        box_part = f":box=1:boxcolor={_CORNER_BOX_FILL_COLOR}@{_CORNER_BOX_ALPHA}:boxborderw={padding}"

    enable_part = ""
    if content == "transition":
        countdown_start = max(0.0, clip_dur - _TRANSITION_COUNTDOWN_SECONDS)
        enable_part = f":enable='gte(t\\,{countdown_start:.6f})'"

    return (
        f"drawtext=text='{text}':fontsize={fontsize}:fontcolor={osd.ffmpeg_color()}:"
        f"x={x}:y={y}{box_part}{enable_part}"
    )


def build_corner_accent_stripe_graph(
    input_label: str,
    output_label: str,
    content: str,
    entry: TimelineEntry,
    output: OutputConfig,
    osd: OsdConfig,
    corner: Corner,
    clip_dur: float,
    label_prefix: str,
) -> Optional[list[str]]:
    """Return a list of `;`-joinable filtergraph statements from
    `input_label` to `[{output_label}]` that draw a thin, genuinely
    semi-transparent (`_ACCENT_STRIPE_ALPHA`) vertical accent stripe flush
    with the corner box's left edge (left corners) or right edge (right
    corners), in `OsdCornerBoxConfig.color` -- or None when there's no box
    to accent (corner_box disabled, or this entry has nothing to show for
    the configured content type).

    Why this needs 4 filter stages instead of one `drawtext`: the stripe's
    HEIGHT must match the real corner box's height exactly, but ffmpeg only
    computes that height (from real glyph metrics) as a side effect of
    actually rendering a `drawtext` box -- there is no way to ask for it as
    a number. So this renders a full duplicate box (same text/fontsize/
    boxborderw as the real corner text, guaranteeing an identical height),
    shifted `stripe_w` outward, then CROPS that render down to just the
    `stripe_w`-wide sliver that sits outside the real box's footprint,
    discarding the rest before it ever reaches the frame.

    This crop step is what keeps the stripe's transparency genuine: an
    earlier version instead relied on the real box being drawn ON TOP of
    the full duplicate box to occlude the overlapping majority of it. That
    doesn't actually remove the overlap -- both boxes are semi-transparent,
    so wherever they occupied the same pixels, BOTH colors blended into the
    final result, visibly tinting the "fixed" fill toward the border color
    (confirmed in real ffmpeg output). Physically discarding the
    non-sliver portion via `crop`, before compositing anything back onto
    the frame, means the fill (drawn afterward, in build_osd_filters) and
    the stripe never occupy the same pixel -- each blends independently
    with the ORIGINAL video only, so both stay semi-transparent with no
    cross-contamination.

    The crop/overlay position needs no unknown text-width term: a left
    corner's box LEFT edge is `margin - padding` regardless of text width
    (the real text's own x is anchored at `margin`), and a right corner's
    box RIGHT edge is `output.width - margin + padding` regardless of text
    width (the real text's `x=w-tw-margin` cancels the unknown `tw` exactly
    against the box's own `+tw` extent) -- so the crop only needs the
    (known) HEIGHT-matching trick, not a width/position one. The crop
    itself only touches x (a full-height, `h=ih` column), so no vertical
    positioning knowledge is needed either -- row alignment is preserved
    automatically.

    `exact=1` on the crop is required: by default (`exact=0`) ffmpeg's crop
    filter silently rounds w/x down to fit 4:2:0 chroma subsampling (an
    even-pixel grid), shrinking our exactly-computed `stripe_w`-wide window
    by a pixel or two -- since the fill is positioned independently at its
    own exact coordinate (unaffected by this rounding), that shrink could
    open a visible sliver of untouched background between the two,
    confirmed by direct pixel sampling of real (h264/yuv420p) ffmpeg
    output. `exact=1` crops at the literal requested pixels rather than the
    rounded ones.

    On top of that, the crop/overlay window is deliberately widened by
    `_ACCENT_STRIPE_OVERLAP_PX` on the side facing the fill box, so the
    stripe always paints `_ACCENT_STRIPE_OVERLAP_PX` pixels INTO the box's
    own footprint rather than merely landing flush against it. Combined
    with the stripe now being fully opaque (`_ACCENT_STRIPE_ALPHA == 1.0`),
    this means even a residual off-by-a-pixel mismatch between the two
    independently-positioned elements is covered by the stripe's own solid
    color instead of exposing a gap -- belt-and-suspenders alongside
    `exact=1`, not a replacement for it.
    """
    if not osd.corner_box.enabled:
        return None
    text = _corner_content_text(content, entry, clip_dur, osd)
    if text is None:
        return None

    fontsize, margin, bottom_offset = _corner_geometry(output, osd)
    padding = round(fontsize * _CORNER_BOX_PADDING_FRACTION)
    stripe_w = max(2, round(fontsize * _ACCENT_STRIPE_WIDTH_FRACTION))
    crop_w = stripe_w + _ACCENT_STRIPE_OVERLAP_PX
    y = f"{margin}" if "top" in corner else f"h-th-{bottom_offset}"
    color = to_ffmpeg_color(osd.corner_box.color)

    if "left" in corner:
        shifted_x = f"{margin - stripe_w}"
        crop_x = margin - padding - stripe_w
    else:
        shifted_x = f"w-tw-{margin - stripe_w}"
        crop_x = output.width - margin + padding - _ACCENT_STRIPE_OVERLAP_PX

    enable_part = ""
    if content == "transition":
        countdown_start = max(0.0, clip_dur - _TRANSITION_COUNTDOWN_SECONDS)
        enable_part = f":enable='gte(t\\,{countdown_start:.6f})'"

    main = f"{label_prefix}main"
    full = f"{label_prefix}full"
    boxed = f"{label_prefix}boxed"
    col = f"{label_prefix}col"

    return [
        f"{input_label} split=2 [{main}][{full}]",
        f"[{full}] drawtext=text='{text}':fontsize={fontsize}:fontcolor=black@0.0:"
        f"x={shifted_x}:y={y}:box=1:boxcolor={color}@{_ACCENT_STRIPE_ALPHA}:boxborderw={padding}{enable_part} [{boxed}]",
        f"[{boxed}] crop=w={crop_w}:h=ih:x={crop_x}:y=0:exact=1 [{col}]",
        f"[{main}][{col}] overlay=x={crop_x}:y=0 [{output_label}]",
    ]


def build_osd_filters(
    entry: TimelineEntry,
    output: OutputConfig,
    osd: Optional[OsdConfig],
    input_label: str,
) -> tuple[list[str], str]:
    """Top-level orchestrator: returns `(graph_lines, output_label)`.
    `graph_lines` is the ordered list of `;`-joinable filtergraph statements
    connecting `input_label` through this entry's OSD (bar first, then
    corners in a fixed order), or `[]` when there's nothing to draw (OSD
    unset/disabled globally, or suppressed for this asset via `no_osd`) --
    in which case `output_label` is just `input_label` unchanged, so the
    caller can treat this call uniformly whether or not OSD contributed
    anything.

    A plain list of self-contained bare filter strings (the pre-existing
    interface) can't express the countdown bar, which needs a branching
    sub-graph (color source + overlay, see build_progress_bar_graph) rather
    than a single one-input-one-output filter -- so this manages its own
    pad labels end-to-end instead of leaving that to the caller.
    """
    if osd is None or not osd.enabled or entry.no_osd:
        return [], input_label

    clip_dur = entry.clip_duration
    lines: list[str] = []
    current = input_label
    step = 0

    if osd.countdown.enabled:
        next_label = f"osd{step}"
        lines.extend(
            build_progress_bar_graph(
                current, next_label, clip_dur, output, osd.countdown.height_pct, f"osdbar{step}_"
            )
        )
        current = f"[{next_label}]"
        step += 1

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
        if f is None:
            continue
        next_label = f"osd{step}"
        stripe_lines = build_corner_accent_stripe_graph(
            current, next_label, content, entry, output, osd, corner, clip_dur, f"osdstripe{step}_"
        )
        if stripe_lines is not None:
            lines.extend(stripe_lines)
            current = f"[{next_label}]"
            step += 1
        next_label = f"osd{step}"
        lines.append(f"{current} {f} [{next_label}]")
        current = f"[{next_label}]"
        step += 1

    return lines, current
