"""Channel-level `period_on_segmentation` (SCOPE.md §14): forced, signal-only
Period / #EXT-X-DISCONTINUITY at markers with chosen segmentation_type_ids,
in both timeline modes. Signal only: media timestamps never restart because
of it, so continuous mode keeps its absolute `t` / tfdt axis."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from serve import (  # noqa: E402
    Channel,
    compute_discontinuity_sequence,
    compute_signal_breaks,
    parse_segmentation_type_ids,
)
from test_serve_asset_boundaries import _sparse_fake_package  # noqa: E402

TICKS = [0, 90_000, 180_000, 270_000]  # 1s segments, 4 per loop
LOOP = 360_000


def _marker(event_id: str, pts: int, type_id: str | None, is_out: bool = True) -> dict:
    return {
        "event_id": event_id,
        "pts_time_ticks": pts,
        "segmentation_type_id": type_id,
        "is_out": is_out,
        "splice_type": "time_signal",
        "splice_command_b64": "AA==",
        "splice_command_b64_narrowed": "AA==",
    }


MARKERS = [
    _marker("0x1", 180_000, "0x22"),  # break start -> segment 2
    _marker("0x2", 90_000, "0x01"),   # something else, never forced
]


def _channel(type_ids, *, continuous: bool, markers=MARKERS, now_ticks=None) -> Channel:
    package = _sparse_fake_package(
        boundaries=[], gap_ticks_by_index={}, segment_boundary_ticks=TICKS, total_loop_duration_ticks=LOOP
    )
    package.markers = markers
    channel = Channel(package, epoch_ticks=0, window_segments=6, period_on_segmentation=frozenset(type_ids))
    channel.continuous = continuous  # the fake package has no real tfdt to validate
    if now_ticks is not None:
        channel.now_ticks = lambda: now_ticks
    return channel


def _hls(channel: Channel) -> list[str]:
    return channel.build_hls_manifest("archive").splitlines()


# ── parsing / index resolution ──────────────────────────────────────────────


def test_parse_segmentation_type_ids():
    assert parse_segmentation_type_ids("0x22, 0x30,49") == frozenset({0x22, 0x30, 49})
    assert parse_segmentation_type_ids("") == frozenset()
    assert parse_segmentation_type_ids(None) == frozenset()
    with pytest.raises(ValueError):
        parse_segmentation_type_ids("0x22,nope")
    with pytest.raises(ValueError):
        parse_segmentation_type_ids("0x100")


def test_signal_breaks_resolve_to_containing_segment_and_ignore_untyped():
    markers = MARKERS + [
        _marker("0x3", 200_000, "0x22"),  # inside segment 2 too
        _marker("0x4", 0, None),          # plain splice_insert: no type id
        {**_marker("0x5", 270_000, "0x23"), "segmentation_type_id": 0x23},  # int form
    ]
    assert compute_signal_breaks(markers, TICKS, frozenset({0x22, 0x23})) == frozenset({2, 3})
    assert compute_signal_breaks(markers, TICKS, frozenset()) == frozenset()


def test_discontinuity_sequence_without_loop_wrap_entry():
    # continuous + signal-only break at local 2 of 4: seq bumps at 2, never at the wrap
    seqs = [compute_discontinuity_sequence(g, 4, {2}) for g in range(9)]
    assert seqs == [0, 0, 1, 1, 1, 1, 2, 2, 2]
    # break at 0 doubles as the loop start: bump exactly at each wrap
    assert [compute_discontinuity_sequence(g, 4, {0}) for g in range(9)] == [0, 0, 0, 0, 1, 1, 1, 1, 2]


# ── HLS ─────────────────────────────────────────────────────────────────────


def test_hls_continuous_without_option_has_no_discontinuity():
    lines = _hls(_channel([], continuous=True, now_ticks=LOOP + 270_000))
    assert "#EXT-X-DISCONTINUITY" not in lines
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:0" in lines


def test_hls_continuous_forces_discontinuity_only_at_the_break():
    # live edge = global 7 (loop 1, local 3): window 2..7 -> breaks at global 2 (first entry) and 6
    lines = _hls(_channel([0x22], continuous=True, now_ticks=LOOP + 270_000))
    assert lines.count("#EXT-X-DISCONTINUITY") == 1  # global 6; global 2 is the window's first entry
    seq = next(line for line in lines if line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE"))
    assert seq == "#EXT-X-DISCONTINUITY-SEQUENCE:1"
    disc_at = lines.index("#EXT-X-DISCONTINUITY")
    assert [u for u in lines[disc_at:] if "/cseg/" in u][0].endswith("/6.m4s")


def test_hls_default_mode_adds_break_to_loop_wrap():
    lines = _hls(_channel([0x22], continuous=False, now_ticks=LOOP + 270_000))
    # window 2..7: discontinuities before global 4 (wrap) and global 6 (break)
    assert lines.count("#EXT-X-DISCONTINUITY") == 2


# ── DASH ────────────────────────────────────────────────────────────────────


def _periods(mpd: str) -> list[tuple[str, str]]:
    return re.findall(r'<Period id="([^"]+)" start="([^"]+)"', mpd)


def _pto_and_number(mpd: str) -> list[tuple[str, str]]:
    return re.findall(r'startNumber="(\d+)"(?: presentationTimeOffset="(\d+)")?', mpd)


def test_dash_continuous_without_option_is_single_period():
    mpd = _channel([], continuous=True, now_ticks=LOOP + 270_000).build_dash_manifest(window_segments=6)
    assert _periods(mpd) == [("continuous", "PT0S")]
    assert "presentationTimeOffset" not in mpd


def test_dash_continuous_splits_into_periods_keeping_absolute_timeline():
    mpd = _channel([0x22], continuous=True, now_ticks=LOOP + 270_000).build_dash_manifest(window_segments=6)
    # window globals 2..7; breaks open a Period at global 2 and global 6 (abs 180000 / 540000)
    assert _periods(mpd) == [("break2", "PT2.0S"), ("break6", "PT6.0S")]
    assert _pto_and_number(mpd) == [("2", "180000"), ("6", "540000")]
    # t stays the absolute media position in every Period (== the rewritten tfdt)
    ts = [int(t) for t in re.findall(r'<S t="(\d+)"', mpd)]
    assert ts == [180_000, 270_000, 360_000, 450_000, 540_000, 630_000]


def test_dash_continuous_window_before_first_break_keeps_initial_period():
    mpd = _channel([0x22], continuous=True, now_ticks=90_000).build_dash_manifest(window_segments=6)
    assert _periods(mpd) == [("continuous", "PT0S")]


def test_dash_continuous_places_marker_event_in_the_period_it_opens():
    mpd = _channel([0x22], continuous=True, now_ticks=LOOP + 270_000).build_dash_manifest(window_segments=6)
    first, second = mpd.split('<Period id="break6"')
    # the 0x22 marker at loop-local 180000 fires in loop 0's Period exactly at its start
    assert 'presentationTime="0"' in first
    assert "<Event" not in second or 'presentationTime="0"' in second  # loop 1's occurrence opens break6


def test_dash_default_mode_signal_break_reuses_media_timeline_via_pto():
    mpd = _channel([0x22], continuous=False, now_ticks=270_000).build_dash_manifest(window_segments=6)
    assert _periods(mpd) == [("loop0-0", "PT0.0S"), ("loop0-2", "PT2.0S")]
    # 2nd Period's t continues from the real span start (loop-relative 180000, 270000),
    # and presentationTimeOffset maps t=180000 to the Period start
    assert _pto_and_number(mpd)[1] == ("2", "180000")
    assert [int(t) for t in re.findall(r'<S t="(\d+)"', mpd)] == [0, 90_000, 180_000, 270_000]


# ── per-format control ──────────────────────────────────────────────────────


def _channel_apply(apply: str, *, continuous: bool) -> Channel:
    package = _sparse_fake_package(
        boundaries=[], gap_ticks_by_index={}, segment_boundary_ticks=TICKS, total_loop_duration_ticks=LOOP
    )
    package.markers = MARKERS
    channel = Channel(
        package, epoch_ticks=0, window_segments=6,
        period_on_segmentation=frozenset({0x22}), period_apply=apply,
    )
    channel.continuous = continuous
    channel.now_ticks = lambda: LOOP + 270_000
    return channel


def test_apply_dash_only_leaves_hls_untouched():
    ch = _channel_apply("dash", continuous=True)
    assert "#EXT-X-DISCONTINUITY" not in _hls(ch)
    assert len(_periods(ch.build_dash_manifest(window_segments=6))) == 2


def test_apply_hls_only_leaves_dash_single_period():
    ch = _channel_apply("hls", continuous=True)
    assert _hls(ch).count("#EXT-X-DISCONTINUITY") == 1
    assert _periods(ch.build_dash_manifest(window_segments=6)) == [("continuous", "PT0S")]


def test_apply_hls_only_default_mode_keeps_real_loop_wrap_in_dash():
    ch = _channel_apply("hls", continuous=False)
    assert _hls(ch).count("#EXT-X-DISCONTINUITY") == 2  # wrap + forced break
    ch.now_ticks = lambda: 270_000
    assert [p for p, _ in _periods(ch.build_dash_manifest(window_segments=6))] == ["loop0"]


def test_apply_rejects_unknown_value():
    with pytest.raises(ValueError):
        _channel_apply("xml", continuous=True)


# ── start implies end ───────────────────────────────────────────────────────


def test_start_type_implies_its_end_and_vice_versa():
    from serve import expand_segmentation_pairs

    assert expand_segmentation_pairs(frozenset({0x22})) == frozenset({0x22, 0x23})
    assert expand_segmentation_pairs(frozenset({0x31})) == frozenset({0x30, 0x31})
    # 0x11 (Program End) closes 0x10/0x17/0x19: ambiguous backwards, left alone
    assert expand_segmentation_pairs(frozenset({0x11})) == frozenset({0x11})
    assert expand_segmentation_pairs(frozenset({0x10})) == frozenset({0x10, 0x11})


def test_listing_only_the_start_breaks_at_the_end_marker_too():
    markers = [_marker("0x1", 90_000, "0x22"), _marker("0x1", 270_000, "0x23", is_out=False)]
    ch = _channel([0x22], continuous=True, markers=markers)
    assert ch.signal_breaks == frozenset({1, 3})
