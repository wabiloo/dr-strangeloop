"""`announce_ahead` / `announce_on`: markers signaled BEFORE the segment they fall in (HLS EXT-X-DATERANGE, DASH
Event), across the loop wrap, kept until every segment they cover has left the window, and re-signaled in later
DASH Periods. Off (0) is byte-identical to before."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from serve import Channel, _marker_covers_segment, resolve_request_channel  # noqa: E402
from test_serve_asset_boundaries import _sparse_fake_package  # noqa: E402
from timeshift import (  # noqa: E402
    TimeshiftConfig,
    TimeshiftError,
    parse_announce_ahead,
    parse_announce_on,
)

TS = 90_000
SEG = TS  # 1 s segments
PER_LOOP = 20
LOOP = PER_LOOP * SEG
TICKS = [i * SEG for i in range(PER_LOOP)]


def _marker(event_id: str, start_s: float, type_id: str | None, duration_s: float | None = None) -> dict:
    return {
        "event_id": event_id,
        "pts_time_ticks": round(start_s * TS),
        "segmentation_type_id": type_id,
        "segmentation_duration_ticks": round(duration_s * TS) if duration_s is not None else None,
        "is_out": type_id in ("0x22", "0x30"),
        "splice_type": "time_signal" if type_id else "splice_insert",
        "splice_command_b64": "AA==",
        "splice_command_b64_narrowed": "AA==",
    }


BREAK_OUT = _marker("0x1", 10, "0x22", duration_s=4)  # covers [10, 14)
BREAK_IN = _marker("0x1", 14, "0x23")  # point at 14
EARLY = _marker("0x2", 2, "0x22", duration_s=3)  # 2 s into the loop: announced in the previous one
SPLICE = _marker("0x3", 16, None)  # plain splice_insert
MARKERS = [EARLY, BREAK_OUT, BREAK_IN, SPLICE]
BREAK = [BREAK_OUT, BREAK_IN]


def _channel(
    *, ahead=0.0, on=frozenset(), splice_insert=False, live_edge: int, window=4, continuous=True,
    markers=MARKERS, split=frozenset(),
) -> Channel:
    package = _sparse_fake_package(
        boundaries=[], gap_ticks_by_index={}, segment_boundary_ticks=TICKS, total_loop_duration_ticks=LOOP
    )
    package.markers = markers
    channel = Channel(
        package, epoch_ticks=0, window_segments=window, period_on_segmentation=split,
        announce_ahead=ahead, announce_on=on, announce_splice_insert=splice_insert,
    )
    channel.continuous = continuous
    channel.now_ticks = lambda: live_edge * SEG + SEG // 2
    return channel


def _anchored(channel: Channel, first: int, last: int) -> dict[int, list[tuple[int, int]]]:
    """segment -> [(marker start in seconds, loop the marker belongs to)]"""
    return {
        g: [(a.marker["pts_time_ticks"] // TS, a.loop) for a in hits]
        for g, hits in channel.marker_anchors(first, last).items()
    }


# ── the cover rule ──────────────────────────────────────────────────────────


def test_zero_lead_is_the_old_rule():
    for marker in (BREAK_OUT, BREAK_IN, SPLICE):
        for s in range(PER_LOOP):
            bounds = (s * SEG, (s + 1) * SEG)
            assert _marker_covers_segment(marker, *bounds) == _marker_covers_segment(marker, *bounds, 0)
    assert _marker_covers_segment(BREAK_IN, 14 * SEG, 15 * SEG)
    assert not _marker_covers_segment(BREAK_IN, 13 * SEG, 14 * SEG)


def test_lead_extends_coverage_backwards_and_keeps_the_old_end():
    lead = 3 * SEG
    # interval marker [10, 14): covered from 7 to its end, as before
    covered = [s for s in range(PER_LOOP) if _marker_covers_segment(BREAK_OUT, s * SEG, (s + 1) * SEG, lead)]
    assert covered == [7, 8, 9, 10, 11, 12, 13]
    # point marker at 14: covered from 11 through the segment that holds it, and no further
    covered = [s for s in range(PER_LOOP) if _marker_covers_segment(BREAK_IN, s * SEG, (s + 1) * SEG, lead)]
    assert covered == [11, 12, 13, 14]


# ── anchoring ───────────────────────────────────────────────────────────────


def test_off_anchors_each_marker_on_its_own_segment_only():
    ch = _channel(live_edge=18, window=18)
    assert _anchored(ch, 1, 18) == {2: [(2, 0)], 10: [(10, 0)], 14: [(14, 0)], 16: [(16, 0)]}


def test_lead_announces_on_the_first_segment_within_the_lead_of_the_live_edge():
    assert _anchored(_channel(ahead=3, live_edge=7, markers=BREAK), 4, 7) == {7: [(10, 0)]}  # 3 s before the marker at 10
    assert _anchored(_channel(ahead=3, live_edge=6, markers=BREAK), 3, 6) == {}  # one segment earlier: not yet


def test_an_announced_marker_stays_while_any_covered_segment_is_in_the_window():
    # window 11..13: the announce segment (7) is long gone, yet both are still covered -> re-anchored on 11
    ch = _channel(ahead=3, live_edge=13, window=3, markers=BREAK)
    assert _anchored(ch, 11, 13) == {11: [(10, 0), (14, 0)]}
    # the break (covers [10, 14)) is over; the End marker, point-in-time at 14, is left ...
    ch = _channel(ahead=3, live_edge=16, window=3, markers=BREAK)
    assert _anchored(ch, 14, 16) == {14: [(14, 0)]}
    # ... and goes with its own segment
    ch = _channel(ahead=3, live_edge=17, window=3, markers=BREAK)
    assert _anchored(ch, 15, 17) == {}


def test_the_announce_segment_is_never_after_the_marker_segment():
    anchors = _channel(ahead=3, live_edge=18, window=18).marker_anchors(1, 18)
    assert anchors
    for g, hits in anchors.items():
        for a in hits:
            assert g <= a.loop * PER_LOOP + a.marker["pts_time_ticks"] // SEG


def test_next_loop_markers_are_announced_in_the_current_loop():
    # EARLY is 2 s into the NEXT loop (22 s); with a 3 s lead it is announced at 19, the last segment of loop 0
    assert _anchored(_channel(ahead=3, live_edge=19, window=3), 17, 19) == {19: [(2, 1)]}
    # still anchored there once the loop has wrapped (its coverage runs to 25 s), once, keyed to loop 1
    assert _anchored(_channel(ahead=3, live_edge=22, window=6), 17, 22) == {19: [(2, 1)]}


def test_a_lead_longer_than_a_loop_reaches_two_loops_ahead():
    ch = _channel(ahead=25, live_edge=0, window=1, markers=[BREAK_OUT])  # loop 1's break (30 s) is announced at 5 s
    assert {g: [a.loop for a in h] for g, h in ch.marker_anchors(0, 0).items()} == {0: [0]}
    ch = _channel(ahead=35, live_edge=0, window=1, markers=[BREAK_OUT])  # ... and at -5 s with a 35 s lead
    assert {g: [a.loop for a in h] for g, h in ch.marker_anchors(0, 0).items()} == {0: [0, 1]}


@pytest.mark.parametrize(
    ("on", "splice", "expected"),
    [
        (frozenset(), False, {"0x1", "0x2", "0x3"}),  # nothing selected: every marker
        (frozenset({0x22}), False, {"0x1", "0x2"}),  # the 0x22 break (and its 0x23 end) only
        (frozenset(), True, {"0x3"}),  # splice_insert only
        (frozenset({0x22}), True, {"0x1", "0x2", "0x3"}),
    ],
)
def test_announce_on_selects_the_markers(on, splice, expected):
    ch = _channel(ahead=3, on=on, splice_insert=splice, live_edge=0, window=1)
    announced = {m["event_id"] for m in MARKERS if ch.marker_announce_ticks(m)}
    assert announced == expected


def test_a_start_implies_its_end():
    ch = _channel(ahead=3, on=frozenset({0x22}), live_edge=0)
    assert ch.marker_announce_ticks(BREAK_IN) == 3 * TS


def test_a_closed_range_never_announces_ahead():
    ch = _channel(ahead=3, live_edge=18, window=18)
    assert ch.marker_anchors(1, 18, announce=False) == _channel(live_edge=18, window=18).marker_anchors(1, 18)


# ── HLS ─────────────────────────────────────────────────────────────────────


def _hls(channel: Channel) -> list[str]:
    return channel.build_hls_manifest("archive").splitlines()


def _dateranges(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("#EXT-X-DATERANGE")]


def test_hls_off_is_unchanged():
    assert _hls(_channel(ahead=0, live_edge=8)) == _hls(_channel(live_edge=8))


def test_hls_carries_the_daterange_ahead_with_the_markers_own_start_date():
    assert _dateranges(_hls(_channel(live_edge=7, markers=BREAK))) == []
    early = _hls(_channel(ahead=3, live_edge=7, markers=BREAK))
    got = _dateranges(early)
    assert len(got) == 1
    # the START-DATE is the marker's own (10 s), not the announce segment's (7 s)
    assert 'START-DATE="1970-01-01T00:00:10.000Z"' in got[0]
    # and the tag sits before the announce segment's PROGRAM-DATE-TIME (7 s)
    assert early[early.index(got[0]) + 1].startswith("#EXT-X-PROGRAM-DATE-TIME:1970-01-01T00:00:07")


def test_hls_daterange_is_identical_on_every_poll_it_is_in():
    a = _dateranges(_hls(_channel(ahead=3, live_edge=7, markers=BREAK)))
    b = _dateranges(_hls(_channel(ahead=3, live_edge=9, markers=BREAK)))
    assert a == b and a


def test_hls_daterange_stays_until_the_covered_segments_are_gone():
    # window 11..13: announced on 7, which has left; the break still covers 11..13
    assert len(_dateranges(_hls(_channel(ahead=3, live_edge=13, window=3, markers=[BREAK_OUT])))) == 1
    # window 14..16: the break [10, 14) is over
    assert _dateranges(_hls(_channel(ahead=3, live_edge=16, window=3, markers=[BREAK_OUT]))) == []


def test_hls_announces_across_the_loop_wrap_with_the_next_loops_start_date():
    got = _dateranges(_hls(_channel(ahead=3, live_edge=19, window=3, markers=[EARLY])))
    assert len(got) == 1
    assert 'START-DATE="1970-01-01T00:00:22.000Z"' in got[0]  # loop 1 starts at 20 s; the marker is 2 s in


def test_hls_does_not_announce_the_marker_twice_when_the_next_loop_starts():
    at_wrap = _dateranges(_hls(_channel(ahead=3, live_edge=21, window=6, markers=[EARLY])))
    assert len(at_wrap) == 1


# ── DASH ────────────────────────────────────────────────────────────────────


def _events(mpd: str) -> list[tuple[int, str]]:
    return [(int(t), i) for t, i in re.findall(r'<Event presentationTime="(-?\d+)"(?: duration="\d+")? id="(\d+)"', mpd)]


def test_dash_continuous_off_is_unchanged():
    ch = _channel(ahead=0, live_edge=8)
    assert ch.build_dash_manifest(window_segments=4) == _channel(live_edge=8).build_dash_manifest(window_segments=4)


def test_dash_continuous_announces_with_the_markers_own_presentation_time():
    assert _events(_channel(live_edge=7, markers=BREAK).build_dash_manifest(window_segments=4)) == []
    got = _events(_channel(ahead=3, live_edge=7, markers=BREAK).build_dash_manifest(window_segments=4))
    assert [t for t, _ in got] == [10 * TS]


def test_dash_continuous_keeps_the_event_until_its_covered_segments_are_gone():
    mpd = _channel(ahead=3, live_edge=13, window=3, markers=BREAK).build_dash_manifest(window_segments=3)
    assert [t for t, _ in _events(mpd)] == [10 * TS, 14 * TS]
    mpd = _channel(ahead=3, live_edge=17, window=3, markers=BREAK).build_dash_manifest(window_segments=3)
    assert _events(mpd) == []


def test_dash_continuous_next_loop_marker_gets_its_own_loops_id_and_time():
    got = _events(_channel(ahead=3, live_edge=19, window=3, markers=[EARLY]).build_dash_manifest(window_segments=3))
    assert [t for t, _ in got] == [LOOP + 2 * TS]
    on_air = _events(_channel(live_edge=22, window=3, markers=[EARLY]).build_dash_manifest(window_segments=3))
    assert got == on_air  # the same id it has when it is on air


def test_dash_continuous_resignals_in_the_period_a_forced_break_opens():
    # 0x22 forces a Period at 10 (and 14, 0x23). The break is announced in the Period before it and carried again by
    # the Period it opens, with the same id and a presentationTime relative to that Period.
    ch = _channel(ahead=3, live_edge=11, window=6, split=frozenset({0x22}), markers=[BREAK_OUT])
    mpd = ch.build_dash_manifest(window_segments=6)
    assert [p for p, _ in re.findall(r'<Period id="([^"]+)" start="([^"]+)"', mpd)] == ["continuous", "break10"]
    first, second = mpd.split('<Period id="break10"')
    (t1, id1), (t2, id2) = _events(first)[0], _events(second)[0]
    assert id1 == id2
    assert (t1, t2) == (10 * TS, 0)


def test_dash_continuous_without_a_lead_still_signals_a_break_once():
    ch = _channel(ahead=0, live_edge=11, window=6, split=frozenset({0x22}), markers=[BREAK_OUT])
    assert len(_events(ch.build_dash_manifest(window_segments=6))) == 1  # in the Period it opens


def test_dash_continuous_resignal_stops_with_the_covered_segments():
    # window 14..17: Period break14 (0x23 forces one too) is the only one with a segment the break still covers? no:
    # the break covers [10, 14), so nothing in 14..17 -- the event is gone from every Period
    ch = _channel(ahead=3, live_edge=17, window=4, split=frozenset({0x22}), markers=[BREAK_OUT])
    assert _events(ch.build_dash_manifest(window_segments=4)) == []


def test_dash_periodic_announces_the_next_loops_marker_in_the_last_period():
    ch = _channel(ahead=3, live_edge=19, window=3, continuous=False, markers=[EARLY])
    # the Period starts at loop 0 (0 s): the next loop's marker (2 s into loop 1) is at 22 s, past the Period's end
    assert [t for t, _ in _events(ch.build_dash_manifest(window_segments=3))] == [LOOP + 2 * TS]


def test_dash_periodic_carries_it_again_in_the_period_it_belongs_to():
    ch = _channel(ahead=3, live_edge=21, window=6, continuous=False, markers=[EARLY])
    mpd = ch.build_dash_manifest(window_segments=6)
    first, second = mpd.split('<Period id="loop1"')
    (t1, id1), (t2, id2) = _events(first)[0], _events(second)[0]
    assert id1 == id2 and (t1, t2) == (LOOP + 2 * TS, 2 * TS)


def test_dash_periodic_off_has_no_events_for_the_next_loop():
    ch = _channel(ahead=0, live_edge=19, window=3, continuous=False, markers=[EARLY])
    assert _events(ch.build_dash_manifest(window_segments=3)) == []


# ── parsing and per-request overrides ───────────────────────────────────────


def test_parse_announce_ahead():
    assert parse_announce_ahead("10", "x") == 10
    assert parse_announce_ahead("0", "x") == 0
    assert parse_announce_ahead("PT7.5S", "x") == 7.5
    for bad in ("-1", "abc", "99999"):
        with pytest.raises(TimeshiftError):
            parse_announce_ahead(bad, "x")


def test_parse_announce_on():
    assert parse_announce_on("0x22, 0x30", "x") == (frozenset({0x22, 0x30}), False)
    assert parse_announce_on("splice_insert", "x") == (frozenset(), True)
    assert parse_announce_on("0x22,splice_insert", "x") == (frozenset({0x22}), True)
    assert parse_announce_on("", "x") == parse_announce_on("all", "x") == parse_announce_on("none", "x") == (frozenset(), False)
    for bad in ("0x22,nope", "0x100"):
        with pytest.raises(TimeshiftError):
            parse_announce_on(bad, "x")


def _requested(base: Channel | None = None, *, overrides=True, **params) -> Channel:
    return resolve_request_channel(base or _channel(live_edge=8), params, TimeshiftConfig(overrides=overrides), "hls")


def test_query_overrides_the_channel_setting():
    assert _requested(announce_ahead="5").announce_ahead_ticks == 5 * TS
    assert _requested().announce_ahead_ticks == 0
    ch = _requested(announce_ahead="5", announce_on="0x22,splice_insert")
    assert (ch.announce_on, ch.announce_splice_insert) == (frozenset({0x22, 0x23}), True)


def test_query_can_switch_a_channels_lead_off_and_change_only_the_types():
    base = _channel(ahead=4, live_edge=8)
    assert _requested(base, announce_ahead="0").announce_ahead_ticks == 0
    changed = _requested(base, announce_on="0x30")
    assert changed.announce_ahead_ticks == 4 * TS and changed.announce_on == frozenset({0x30, 0x31})
    assert base.announce_on == frozenset()  # the channel itself is never mutated


def test_query_is_ignored_with_overrides_off():
    assert _requested(overrides=False, announce_ahead="5").announce_ahead_ticks == 0


def test_bad_query_values_are_a_400():
    with pytest.raises(TimeshiftError):
        _requested(announce_ahead="-3")
    with pytest.raises(TimeshiftError):
        _requested(announce_on="0xZZ")


def test_the_constructor_rejects_an_out_of_range_lead():
    with pytest.raises(ValueError):
        _channel(ahead=-1, live_edge=0)
    with pytest.raises(ValueError):
        _channel(ahead=10**6, live_edge=0)
