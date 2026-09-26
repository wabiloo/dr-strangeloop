"""Tests for the OSD (on-screen display) feature: config schema, span
coverage / is_adbreak resolution, next_asset_id wraparound, abbreviation
derivation, and ffmpeg filter-string construction."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from franken_ts.cache import entry_cache_key
from franken_ts.config import AssetConfig, Config, MarkerConfig, OsdConfig, to_ffmpeg_color
from franken_ts.osd import (
    abbreviation_for_marker,
    build_corner_accent_stripe_graph,
    build_corner_text_filter,
    build_osd_filters,
    build_progress_bar_graph,
)
from franken_ts.timeline import TimelineEntry, build_timeline

from tests.test_markers import _entries_from_config, _nested_break_config


class _FakeInfo:
    def __init__(self, duration: float):
        self.duration = duration


def _output(width: int = 1920, height: int = 1080, framerate: int = 25):
    from franken_ts.config import OutputConfig
    return OutputConfig(file="out.ts", resolution=f"{width}x{height}", framerate=framerate)


# ── Schema defaults ──────────────────────────────────────────────────────

def test_osd_config_defaults():
    osd = OsdConfig()
    assert osd.enabled is False
    assert osd.countdown.enabled is True
    assert osd.countdown.height_pct == 3.0
    assert osd.text_size_pct == 3.0
    assert osd.text_color == "#FFFFFF"
    assert osd.ad_break_label == "ad break"
    assert osd.corners.top_left is None
    assert osd.corners.top_right is None
    assert osd.corners.bottom_left == "asset_id"
    assert osd.corners.bottom_right == "time"
    assert osd.corner_box.enabled is False
    assert osd.corner_box.color == "#000000"


def test_osd_config_invalid_corner_value_rejected():
    with pytest.raises(ValidationError):
        OsdConfig.model_validate({"corners": {"top_left": "not_a_real_content_type"}})


def test_osd_config_invalid_text_color_rejected():
    with pytest.raises(ValidationError):
        OsdConfig.model_validate({"text_color": "white"})


def test_osd_config_invalid_corner_box_color_rejected():
    with pytest.raises(ValidationError):
        OsdConfig.model_validate({"corner_box": {"color": "black"}})


def test_osd_config_ffmpeg_color():
    assert OsdConfig(text_color="#AABBCC").ffmpeg_color() == "0xAABBCC"


def test_to_ffmpeg_color():
    assert to_ffmpeg_color("#112233") == "0x112233"


def test_asset_no_osd_defaults_false():
    assert AssetConfig(file="a.mp4").no_osd is False
    assert AssetConfig.model_validate({"file": "a.mp4", "no_osd": True}).no_osd is True


# ── Span resolution / is_adbreak (reusing test_markers's nested fixture) ───

def test_covering_spans_outermost_first():
    cfg = Config.model_validate(_nested_break_config())
    entries = _entries_from_config(cfg)
    from franken_ts.timeline import compute_marker_spans, spans_covering
    spans = compute_marker_spans(cfg.markers, entries)

    jingle_idx = 1  # covered by break + ppo1, not by any "ad"-lane marker
    ad1_idx = 2      # covered by break + ppo2 + ad(0x30)

    jingle_types = [s.marker.event_id for s in spans_covering(jingle_idx, spans)]
    assert jingle_types == [100, 101]  # break (depth 0), ppo1 (depth 1)

    ad1_types = [s.marker.event_id for s in spans_covering(ad1_idx, spans)]
    assert ad1_types == [100, 102, 103]  # break, ppo2, ad -- outermost first


def test_build_timeline_sets_covering_spans_and_is_adbreak():
    cfg = Config.model_validate(_nested_break_config())
    infos = {a.file: _FakeInfo(10.0) for a in cfg.assets}
    entries, _ = build_timeline(cfg.assets, infos, framerate=25, markers=cfg.markers)

    # content1 (index 0): no covering span at all.
    assert entries[0].covering_spans == []
    assert entries[0].is_adbreak is False

    # jingle (index 1): break + ppo -- both non-"custom" lanes -> is_adbreak True.
    jingle_abbrevs = [m.event_id for m in entries[1].covering_spans]
    assert jingle_abbrevs == [100, 101]
    assert entries[1].is_adbreak is True

    # ad1 (index 2): break + ppo + ad, outermost first.
    ad1_event_ids = [m.event_id for m in entries[2].covering_spans]
    assert ad1_event_ids == [100, 102, 103]
    assert entries[2].is_adbreak is True


def test_is_adbreak_false_for_custom_lane_marker():
    """A marker whose segmentation type isn't break/ppo/ad-related (e.g.
    0x10 Program Start) yields `type == "custom"`, so it must NOT count toward
    is_adbreak."""
    raw = {
        "output": {"file": "out.ts"},
        "assets": [
            {"file": "c1.mp4", "id": "c1"},
        ],
        "markers": [
            {"event_id": 1, "splice_type": "time_signal",
             "segmentation": {"type_id": "0x10", "upid_hex": "aa"}, "assets": ["c1"]},
        ],
    }
    cfg = Config.model_validate(raw)
    infos = {a.file: _FakeInfo(10.0) for a in cfg.assets}
    entries, _ = build_timeline(cfg.assets, infos, framerate=25, markers=cfg.markers)
    assert len(entries[0].covering_spans) == 1
    assert entries[0].is_adbreak is False


def test_program_breakaway_included_as_non_instant_covering_span():
    """Program Breakaway pairs with Program Resumption and spans its assets."""
    raw = {
        "output": {"file": "out.ts"},
        "assets": [
            {"file": "c1.mp4", "id": "c1"},
            {"file": "ad1.mp4", "id": "ad1"},
        ],
        "markers": [
            {"event_id": 1, "splice_type": "time_signal",
             "segmentation": {"type_id": "0x13", "upid_hex": "aa"}, "assets": ["ad1"]},
        ],
    }
    cfg = Config.model_validate(raw)
    infos = {a.file: _FakeInfo(10.0) for a in cfg.assets}
    entries, _ = build_timeline(cfg.assets, infos, framerate=25, markers=cfg.markers)
    assert len(entries[1].covering_spans) == 1
    assert entries[1].is_adbreak is False


# ── next_asset_id (wraparound, skip-stills, fallback to file stem) ────────

def _asset_infos(assets):
    return {a.file: _FakeInfo(10.0) for a in assets}


def test_next_asset_id_skips_stills_and_wraps_around():
    raw = {
        "output": {"file": "out.ts"},
        "assets": [
            {"file": "content.mp4", "id": "content"},
            {"file": "slate.png"},  # still image -- skipped
            {"file": "ad.mp4", "id": "ad"},
        ],
    }
    cfg = Config.model_validate(raw)
    entries, _ = build_timeline(cfg.assets, _asset_infos(cfg.assets), framerate=25)

    assert entries[0].next_asset_id == "ad"       # skips the still image
    assert entries[2].next_asset_id == "content"  # wraps around to the start (playlist loops)


def test_next_asset_id_falls_back_to_file_stem():
    raw = {
        "output": {"file": "out.ts"},
        "assets": [
            {"file": "content.mp4", "id": "content"},
            {"file": "unnamed_ad.mp4"},  # no id
        ],
    }
    cfg = Config.model_validate(raw)
    entries, _ = build_timeline(cfg.assets, _asset_infos(cfg.assets), framerate=25)
    assert entries[0].next_asset_id == "unnamed_ad"


def test_no_osd_propagation():
    raw = {
        "output": {"file": "out.ts"},
        "assets": [
            {"file": "content.mp4", "no_osd": True},
            {"file": "ad.mp4"},
        ],
    }
    cfg = Config.model_validate(raw)
    entries, _ = build_timeline(cfg.assets, _asset_infos(cfg.assets), framerate=25)
    assert entries[0].no_osd is True
    assert entries[1].no_osd is False


def test_osd_label_propagation():
    raw = {
        "output": {"file": "out.ts"},
        "assets": [
            {"file": "content.mp4", "osd_label": "Weather"},
            {"file": "ad.mp4"},
        ],
    }
    cfg = Config.model_validate(raw)
    entries, _ = build_timeline(cfg.assets, _asset_infos(cfg.assets), framerate=25)
    assert entries[0].osd_label == "Weather"
    assert entries[1].osd_label is None


# ── Abbreviation derivation ──────────────────────────────────────────────

def _marker(splice_type="time_signal", type_id=None):
    kwargs = {"event_id": 1, "splice_type": splice_type, "assets": ["x"]}
    if type_id is not None:
        kwargs["segmentation"] = {"type_id": type_id, "upid_hex": "aa"}
    return MarkerConfig.model_validate(kwargs)


def test_abbreviation_bare_splice_insert():
    assert abbreviation_for_marker(_marker(splice_type="splice_insert")) == "SPI"


def test_abbreviation_break():
    assert abbreviation_for_marker(_marker(type_id="0x22")) == "BRK"


def test_abbreviation_provider_placement_opportunity():
    assert abbreviation_for_marker(_marker(type_id="0x34")) == "PPO"


def test_abbreviation_provider_advertisement():
    assert abbreviation_for_marker(_marker(type_id="0x30")) == "PAD"


def test_abbreviation_distributor_advertisement():
    assert abbreviation_for_marker(_marker(type_id="0x32")) == "DAD"


def test_abbreviation_unscheduled_event():
    assert abbreviation_for_marker(_marker(type_id="0x40")) == "USC"


def test_abbreviation_unknown_type_id_falls_back_to_hex():
    assert abbreviation_for_marker(_marker(type_id="0x77")) == "0x77"


# ── Filter construction (pure functions, no ffmpeg invocation) ────────────

def test_build_progress_bar_graph_scales_with_output_height():
    lines_1080 = build_progress_bar_graph(
        "[in]", "barout", 10.0, _output(height=1080), height_pct=5.0, label_prefix="b_"
    )
    lines_360 = build_progress_bar_graph(
        "[in]", "barout", 10.0, _output(height=360), height_pct=5.0, label_prefix="b_"
    )
    graph_1080 = "; ".join(lines_1080)
    graph_360 = "; ".join(lines_360)
    assert "s=1920x54" in graph_1080  # round(1080 * 0.05)
    assert "s=1920x18" in graph_360   # round(360 * 0.05)
    assert "y=1026" in graph_1080     # 1080 - 54
    assert "y=342" in graph_360       # 360 - 18
    assert "\\," in graph_1080        # internal comma in the overlay x= expression is escaped
    assert lines_1080[0].startswith("color=")
    assert lines_1080[1].startswith("[in]")
    assert lines_1080[-1].rstrip().endswith("[barout]")
    assert "colorchannelmixer=aa=0.5" in graph_1080
    assert "overlay=" in graph_1080


def _entry(
    asset_id="a1", is_adbreak=False, covering_spans=None, next_asset_id="a2",
    no_osd=False, osd_label=None,
):
    return TimelineEntry(
        source_file=Path("a.mp4"), inpoint=0.0, outpoint=10.0,
        output_start=0.0, output_end=10.0, inpoint_raw=0.0, outpoint_raw=10.0,
        asset_id=asset_id, next_asset_id=next_asset_id,
        covering_spans=covering_spans or [], is_adbreak=is_adbreak, no_osd=no_osd,
        osd_label=osd_label,
    )


def test_build_corner_text_filter_nothing_to_show_cases():
    osd = OsdConfig()
    output = _output()

    assert build_corner_text_filter("asset_id", _entry(asset_id=None), output, osd, "bottom_left", 10.0) is None
    assert build_corner_text_filter("next_asset_id", _entry(next_asset_id=None), output, osd, "top_right", 10.0) is None
    assert build_corner_text_filter("scte35_spans", _entry(covering_spans=[]), output, osd, "top_left", 10.0) is None
    assert build_corner_text_filter("is_adbreak", _entry(is_adbreak=False), output, osd, "bottom_left", 10.0) is None
    assert build_corner_text_filter("osd_label", _entry(osd_label=None), output, osd, "top_left", 10.0) is None
    assert build_corner_text_filter("osd_label", _entry(osd_label=""), output, osd, "top_left", 10.0) is None


def test_build_corner_text_filter_shows_content():
    osd = OsdConfig()
    output = _output()

    f = build_corner_text_filter("asset_id", _entry(asset_id="a1"), output, osd, "bottom_left", 10.0)
    assert f is not None and "text='a1'" in f
    assert f"fontcolor={osd.ffmpeg_color()}" in f

    f = build_corner_text_filter("next_asset_id", _entry(next_asset_id="a2"), output, osd, "top_right", 10.0)
    assert "next\\: a2" in f

    f = build_corner_text_filter("osd_label", _entry(osd_label="Weather"), output, osd, "top_left", 10.0)
    assert f is not None and "text='Weather'" in f


def test_build_corner_text_filter_time_uses_2_decimal_places():
    osd = OsdConfig()
    output = _output()
    f = build_corner_text_filter("time", _entry(), output, osd, "bottom_right", 34.6)
    assert f is not None
    # Whole seconds, then a literal '.', then centiseconds zero-padded to 2
    # digits via eif's width arg, then '/', then the static total formatted
    # to 2 decimals -- e.g. renders as "12.32/34.60" once ffmpeg evaluates it.
    assert "%{eif\\:trunc(t)\\:d}.%{eif\\:trunc(mod(t\\,1)*100)\\:d\\:2}/34.60" in f


def test_build_corner_text_filter_is_adbreak_uses_configured_label():
    osd = OsdConfig(ad_break_label="AD BREAK IN PROGRESS")
    output = _output()
    f = build_corner_text_filter("is_adbreak", _entry(is_adbreak=True), output, osd, "bottom_left", 10.0)
    assert "AD BREAK IN PROGRESS" in f


def test_build_corner_text_filter_box_disabled_by_default():
    osd = OsdConfig()
    output = _output()
    f = build_corner_text_filter("asset_id", _entry(asset_id="a1"), output, osd, "bottom_left", 10.0)
    assert "box=1" not in f


def test_build_corner_text_filter_box_fill_is_fixed_dark_gray():
    """The box's fill is a fixed, semi-transparent dark gray, never the
    configured corner_box.color -- that field now controls only the border
    accent stripe (build_corner_accent_stripe_graph), not the fill."""
    output = _output()
    for color in ("#FFFFFF", "#000000", "#112233", "#ABCDEF"):
        osd = OsdConfig(corner_box={"enabled": True, "color": color})
        f = build_corner_text_filter("asset_id", _entry(asset_id="a1"), output, osd, "bottom_left", 10.0)
        assert f is not None
        assert "box=1:boxcolor=0x262626@0.6:boxborderw=" in f
        assert f"boxcolor={to_ffmpeg_color(color)}" not in f


def test_build_corner_text_filter_box_padding_scales_with_fontsize():
    osd = OsdConfig(corner_box={"enabled": True}, text_size_pct=10.0)
    small = build_corner_text_filter("asset_id", _entry(asset_id="a1"), _output(height=360), osd, "bottom_left", 10.0)
    large = build_corner_text_filter("asset_id", _entry(asset_id="a1"), _output(height=1080), osd, "bottom_left", 10.0)
    # fontsize scales with output.height, and boxborderw is a fixed fraction
    # of fontsize -- so a taller output must produce a larger boxborderw.
    small_pad = int(small.split("boxborderw=")[1])
    large_pad = int(large.split("boxborderw=")[1])
    assert large_pad > small_pad


def test_build_corner_text_filter_bottom_offset_accounts_for_bar():
    output = _output()
    entry = _entry(asset_id="a1")

    osd_with_bar = OsdConfig()  # countdown.enabled defaults True
    osd_without_bar = OsdConfig(countdown={"enabled": False})

    f_with_bar = build_corner_text_filter("asset_id", entry, output, osd_with_bar, "bottom_left", 10.0)
    f_without_bar = build_corner_text_filter("asset_id", entry, output, osd_without_bar, "bottom_left", 10.0)
    assert f_with_bar != f_without_bar


def test_build_corner_text_filter_bottom_gap_matches_top_margin():
    """A bottom corner's clearance above the countdown bar must equal a top
    corner's clearance from the frame's top edge -- not double it."""
    output = _output(height=1080)
    entry = _entry(asset_id="a1")
    osd = OsdConfig()  # countdown.enabled defaults True, height_pct=3.0

    margin = round(1080 * 0.03)
    bar_h = round(1080 * osd.countdown.height_pct / 100)

    top = build_corner_text_filter("asset_id", entry, output, osd, "top_left", 10.0)
    bottom = build_corner_text_filter("asset_id", entry, output, osd, "bottom_left", 10.0)
    assert top is not None and bottom is not None
    assert f"y={margin}" in top
    assert f"y=h-th-{margin + bar_h}" in bottom


def test_build_corner_accent_stripe_graph_disabled_cases():
    output = _output()
    osd_no_box = OsdConfig(corner_box={"enabled": False})
    osd_with_box = OsdConfig(corner_box={"enabled": True})

    assert (
        build_corner_accent_stripe_graph(
            "[in]", "out", "asset_id", _entry(asset_id="a1"), output, osd_no_box, "bottom_left", 10.0, "p_"
        )
        is None
    )
    assert (
        build_corner_accent_stripe_graph(
            "[in]", "out", "asset_id", _entry(asset_id=None), output, osd_with_box, "bottom_left", 10.0, "p_"
        )
        is None
    )


def test_build_corner_accent_stripe_graph_uses_border_color_and_alpha():
    output = _output()
    osd = OsdConfig(corner_box={"enabled": True, "color": "#112233"})
    lines = build_corner_accent_stripe_graph(
        "[in]", "out", "asset_id", _entry(asset_id="a1"), output, osd, "bottom_left", 10.0, "p_"
    )
    assert lines is not None
    graph = "; ".join(lines)
    assert f"boxcolor={to_ffmpeg_color('#112233')}@1.0:" in graph
    # Its own text is invisible -- only the box is wanted (see docstring).
    assert "fontcolor=black@0.0" in graph
    assert lines[0].startswith("[in]")
    assert "split=2" in lines[0]
    assert lines[-1].rstrip().endswith("[out]")


def test_corner_box_fill_stays_semi_transparent_and_stripe_is_opaque():
    """The fill stays genuinely semi-transparent (0.6) -- video content
    should remain partially visible through it. The border stripe, by
    contrast, is deliberately fully opaque (1.0) and overlaps the fill's
    own footprint (see build_corner_accent_stripe_graph's docstring): this
    guarantees the stripe masks any residual rounding mismatch between the
    two independently-positioned elements instead of exposing a gap."""
    output = _output()
    osd = OsdConfig(corner_box={"enabled": True, "color": "#F7A239"})
    entry = _entry(asset_id="a1")

    fill = build_corner_text_filter("asset_id", entry, output, osd, "bottom_left", 10.0)
    stripe_lines = build_corner_accent_stripe_graph(
        "[in]", "out", "asset_id", entry, output, osd, "bottom_left", 10.0, "p_"
    )
    assert fill is not None and stripe_lines is not None
    graph = "; ".join(stripe_lines)
    assert "boxcolor=0x262626@0.6:" in fill
    assert f"boxcolor={to_ffmpeg_color('#F7A239')}@1.0:" in graph


def test_build_corner_accent_stripe_graph_matches_real_box_height_exactly():
    """The duplicate box drawn inside the stripe graph uses the SAME
    text/fontsize/boxborderw as the real corner text, so ffmpeg computes an
    identical box height for both -- no independent height estimate that
    could drift from the real box, which was the original bug here."""
    output = _output()
    osd = OsdConfig(corner_box={"enabled": True}, text_size_pct=10.0)
    entry = _entry(asset_id="a1")

    stripe_lines = build_corner_accent_stripe_graph(
        "[in]", "out", "asset_id", entry, output, osd, "bottom_left", 10.0, "p_"
    )
    real = build_corner_text_filter("asset_id", entry, output, osd, "bottom_left", 10.0)
    assert stripe_lines is not None and real is not None
    stripe_drawtext = stripe_lines[1]

    def field(f: str, name: str) -> str:
        return f.split(f"{name}=")[1].split(":")[0].split()[0]

    assert field(stripe_drawtext, "fontsize") == field(real, "fontsize")
    assert field(stripe_drawtext, "boxborderw") == field(real, "boxborderw")
    assert field(stripe_drawtext, "y") == field(real, "y")


def test_build_corner_accent_stripe_graph_side_matches_corner():
    output = _output()
    osd = OsdConfig(corner_box={"enabled": True})
    entry = _entry(asset_id="a1")

    left_lines = build_corner_accent_stripe_graph(
        "[in]", "out", "asset_id", entry, output, osd, "bottom_left", 10.0, "p_"
    )
    right_lines = build_corner_accent_stripe_graph(
        "[in]", "out", "asset_id", entry, output, osd, "bottom_right", 10.0, "p_"
    )
    assert left_lines is not None and right_lines is not None
    left = "; ".join(left_lines)
    right = "; ".join(right_lines)

    margin = round(output.height * 0.03)
    fontsize = round(output.height * osd.text_size_pct / 100)
    padding = round(fontsize * 0.3)
    stripe_w = max(2, round(fontsize * 0.22))
    overlap = 2
    crop_w = stripe_w + overlap

    # Left corners: duplicate box shifted stripe_w further left than the
    # real text's own x=margin, cropped at the real box's known left edge
    # minus stripe_w, with the crop window widened by `overlap` pixels so
    # it extends into the real box's footprint rather than stopping flush
    # at its edge. Right corners: shifted stripe_w further right than
    # the real text's own x=w-tw-margin, cropped starting `overlap` pixels
    # inside the real box's known right edge (output.width - margin +
    # padding), for the same reason.
    assert f"x={margin - stripe_w}:" in left
    assert f"x=w-tw-{margin - stripe_w}:" in right
    assert f"crop=w={crop_w}:h=ih:x={margin - padding - stripe_w}:y=0:exact=1" in left
    assert f"crop=w={crop_w}:h=ih:x={output.width - margin + padding - overlap}:y=0:exact=1" in right


def test_build_corner_accent_stripe_graph_overlaps_fill_deliberately():
    """The crop window (the border's visible extent) must extend a small,
    deliberate amount INTO the real fill box's horizontal footprint on the
    side facing it -- this is what guarantees the stripe visually connects
    to the fill with no gap even if the two independently-positioned
    elements are off by a pixel or two (e.g. due to crop rounding)."""
    output = _output()
    osd = OsdConfig(corner_box={"enabled": True}, text_size_pct=8.0)
    entry = _entry(asset_id="a1")

    fontsize = round(output.height * osd.text_size_pct / 100)
    padding = round(fontsize * 0.3)
    margin = round(output.height * 0.03)
    stripe_w = max(2, round(fontsize * 0.22))
    overlap = 2

    left_lines = build_corner_accent_stripe_graph(
        "[in]", "out", "asset_id", entry, output, osd, "bottom_left", 10.0, "p_"
    )
    assert left_lines is not None
    left = "; ".join(left_lines)
    crop_x = margin - padding - stripe_w
    crop_w = stripe_w + overlap
    fill_left_edge = margin - padding
    # The crop's right edge (crop_x + crop_w) must extend `overlap` pixels
    # past the fill's left edge -- i.e. the two regions deliberately
    # overlap rather than merely landing flush.
    assert crop_x + crop_w == fill_left_edge + overlap
    assert f"x={crop_x}:y=0" in left


def test_build_corner_accent_stripe_graph_crop_is_exact():
    """Regression test: the crop MUST use exact=1. Without it, ffmpeg's
    default 4:2:0 chroma-subsampling rounding silently shrinks the
    requested crop width by a pixel or two, opening a visible gap of
    untouched background between the stripe and the fill (which sits at
    its own, unrounded exact coordinate) -- confirmed in real h264/yuv420p
    ffmpeg output, not just a hypothetical."""
    output = _output()
    osd = OsdConfig(corner_box={"enabled": True})
    entry = _entry(asset_id="a1")
    lines = build_corner_accent_stripe_graph(
        "[in]", "out", "asset_id", entry, output, osd, "bottom_left", 10.0, "p_"
    )
    assert lines is not None
    crop_line = next(ln for ln in lines if ln.strip().startswith("[") and "crop=" in ln)
    assert ":exact=1" in crop_line


def test_build_osd_filters_empty_cases():
    output = _output()
    entry = _entry()

    assert build_osd_filters(entry, output, None, "[in]") == ([], "[in]")
    assert build_osd_filters(entry, output, OsdConfig(enabled=False), "[in]") == ([], "[in]")
    assert build_osd_filters(_entry(no_osd=True), output, OsdConfig(enabled=True), "[in]") == ([], "[in]")


def test_build_osd_filters_produces_bar_and_corners():
    output = _output()
    entry = _entry(asset_id="a1")
    osd = OsdConfig(enabled=True)
    lines, final_label = build_osd_filters(entry, output, osd, "[in]")
    # bar (2 graph lines: color source + overlay) + bottom_left (asset_id) +
    # bottom_right (time), one graph line each, by default.
    assert len(lines) == 4
    assert lines[0].startswith("color=")
    assert lines[1].startswith("[in]")
    assert "overlay=" in lines[1]
    assert "drawtext" in lines[2]
    assert "drawtext" in lines[3]
    assert lines[-1].rstrip().endswith(f"[{final_label.strip('[]')}]")


def test_build_osd_filters_inserts_accent_stripe_before_each_corner_with_box_enabled():
    output = _output()
    entry = _entry(asset_id="a1")
    osd = OsdConfig(enabled=True, corner_box={"enabled": True})
    lines, final_label = build_osd_filters(entry, output, osd, "[in]")
    # bar (2 lines) + [stripe graph (4 lines) + real text (1 line)] for
    # bottom_left + the same 5-line group for bottom_right.
    assert len(lines) == 2 + 5 + 5
    bottom_left_stripe = lines[2:6]
    bottom_left_text = lines[6]
    bottom_right_stripe = lines[7:11]
    bottom_right_text = lines[11]

    assert any("fontcolor=black@0.0" in ln for ln in bottom_left_stripe)
    assert "fontcolor=black@0.0" not in bottom_left_text
    assert any("fontcolor=black@0.0" in ln for ln in bottom_right_stripe)
    assert "fontcolor=black@0.0" not in bottom_right_text
    assert lines[-1].rstrip().endswith(f"[{final_label.strip('[]')}]")


# ── Cache key sensitivity ────────────────────────────────────────────────

def test_cache_key_changes_with_is_adbreak_and_next_asset_id(tmp_path):
    output = _output()
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x")

    def make_entry(**overrides):
        base = dict(
            source_file=src, inpoint=0.0, outpoint=10.0,
            output_start=0.0, output_end=10.0, inpoint_raw=0.0, outpoint_raw=10.0,
            asset_id="a1", next_asset_id="a2", is_adbreak=False,
        )
        base.update(overrides)
        return TimelineEntry(**base)

    osd = OsdConfig(enabled=True)
    k1 = entry_cache_key(make_entry(), output, osd)
    k2 = entry_cache_key(make_entry(is_adbreak=True), output, osd)
    k3 = entry_cache_key(make_entry(next_asset_id="different"), output, osd)
    k4 = entry_cache_key(make_entry(osd_label="Weather"), output, osd)
    assert len({k1, k2, k3, k4}) == 4


def test_cache_key_changes_with_osd_config(tmp_path):
    output = _output()
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x")
    entry = TimelineEntry(
        source_file=src, inpoint=0.0, outpoint=10.0,
        output_start=0.0, output_end=10.0, inpoint_raw=0.0, outpoint_raw=10.0,
        asset_id="a1",
    )
    k1 = entry_cache_key(entry, output, OsdConfig(text_size_pct=4.0))
    k2 = entry_cache_key(entry, output, OsdConfig(text_size_pct=6.0))
    k3 = entry_cache_key(entry, output, OsdConfig(text_color="#000000"))
    k4 = entry_cache_key(entry, output, OsdConfig(ad_break_label="different"))
    assert len({k1, k2, k3, k4}) == 4


def test_cache_key_changes_with_no_osd(tmp_path):
    output = _output()
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x")

    def make_entry(no_osd):
        return TimelineEntry(
            source_file=src, inpoint=0.0, outpoint=10.0,
            output_start=0.0, output_end=10.0, inpoint_raw=0.0, outpoint_raw=10.0,
            asset_id="a1", no_osd=no_osd,
        )

    osd = OsdConfig(enabled=True)
    k1 = entry_cache_key(make_entry(False), output, osd)
    k2 = entry_cache_key(make_entry(True), output, osd)
    assert k1 != k2
