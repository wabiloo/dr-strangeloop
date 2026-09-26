"""Tests for extract_dash.py (SCOPE.md §5.1) against synthetic MPD text."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grave_robber.extract_dash import _dash_gap_ticks, extract_dash  # noqa: E402

REAL_SCTE35_B64 = "/DAvAAAAAAAA///wBQb+dGKQoAAZAhdDVUVJSAAAjn+fCAgAAAAALKChijUCAKnMZ1g="

MPD_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" profiles="urn:mpeg:dash:profile:isoff-live:2011"
     type="dynamic" availabilityStartTime="2024-01-01T00:00:00Z" minimumUpdatePeriod="PT2S">
{periods}
</MPD>
"""


def _period(period_id, start, event_streams="", segment_r=1):
    return f"""  <Period id="{period_id}" start="{start}">
{event_streams}
    <AdaptationSet mimeType="video/mp4" segmentAlignment="true">
      <Representation id="v0" bandwidth="5000000" codecs="avc1.640028" width="1920" height="1080">
        <SegmentTemplate media="seg-$Number$.m4s" initialization="init.mp4" timescale="90000" startNumber="0">
          <SegmentTimeline>
            <S t="0" d="540000" r="{segment_r}" />
          </SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
  </Period>"""


def test_extracts_segments_from_segment_timeline():
    mpd = MPD_TEMPLATE.format(periods=_period("p0", "PT0S", segment_r=1))

    segments, markers, boundaries = extract_dash(mpd, "https://cdn.example.com/live/manifest.mpd")

    assert [s.duration_ticks for s in segments] == [540_000, 540_000]
    assert markers == []
    assert boundaries == []


def test_resolves_segment_urls_against_manifest_directory():
    mpd = MPD_TEMPLATE.format(periods=_period("p0", "PT0S", segment_r=0))

    segments, _, _ = extract_dash(mpd, "https://cdn.example.com/live/manifest.mpd")

    assert segments[0].source_uri == "https://cdn.example.com/live/seg-0.m4s"


def test_new_period_sets_asset_boundary_on_its_first_segment():
    mpd = MPD_TEMPLATE.format(
        periods=_period("p0", "PT0S", segment_r=0) + "\n" + _period("p1", "PT6S", segment_r=0)
    )

    segments, _, boundaries = extract_dash(mpd, "https://cdn.example.com/live/manifest.mpd")

    assert [s.asset_boundary for s in segments] == [False, True]
    assert [b.segment_index for b in boundaries] == [1]


def test_period_start_matching_real_cumulative_duration_gives_zero_gap():
    # Period 1 declares start="PT6S" == period 0's real total duration
    # (540000 ticks == 6s) -- no gap/overlap.
    mpd = MPD_TEMPLATE.format(
        periods=_period("p0", "PT0S", segment_r=0) + "\n" + _period("p1", "PT6S", segment_r=0)
    )

    _, _, boundaries = extract_dash(mpd, "https://cdn.example.com/live/manifest.mpd")

    assert boundaries[0].gap_ticks == 0


def test_period_start_ahead_of_real_duration_is_a_positive_gap():
    # Period 0 is 540000 ticks (6s) real, but Period 1 declares start="PT7S"
    # -- a 1s (90000-tick) declared gap.
    mpd = MPD_TEMPLATE.format(
        periods=_period("p0", "PT0S", segment_r=0) + "\n" + _period("p1", "PT7S", segment_r=0)
    )

    _, _, boundaries = extract_dash(mpd, "https://cdn.example.com/live/manifest.mpd")

    assert boundaries[0].gap_ticks == 90_000


def test_period_start_behind_real_duration_is_a_negative_gap_overlap():
    mpd = MPD_TEMPLATE.format(
        periods=_period("p0", "PT0S", segment_r=0) + "\n" + _period("p1", "PT5S", segment_r=0)
    )

    _, _, boundaries = extract_dash(mpd, "https://cdn.example.com/live/manifest.mpd")

    assert boundaries[0].gap_ticks == -90_000


def test_no_explicit_period_start_means_zero_gap():
    """DASH default (no @start attribute at all): starts immediately after
    the previous Period, i.e. no declared gap/overlap. Exercised directly
    against `_dash_gap_ticks` (mpd-inspector's own segment-generation path
    can't resolve `start_time` for a Period with neither an explicit
    @start nor a preceding Period @duration -- an upstream limitation
    unrelated to this function, which only ever reads the raw @start
    attribute, never `start_time`)."""
    fake_period = SimpleNamespace(_tag=SimpleNamespace(start=None))

    assert _dash_gap_ticks(fake_period, period_real_start_ticks=540_000) == 0


def test_eventstream_xml_bin_marker_extracted_with_correct_period_relative_ticks():
    event_stream = f"""    <EventStream schemeIdUri="urn:scte:scte35:2014:xml+bin" timescale="90000" value="0-out">
      <Event presentationTime="180000" duration="2700000" id="1207959695">
        <Signal xmlns="urn:scte:scte35:2013:xml">
          <Binary>{REAL_SCTE35_B64}</Binary>
        </Signal>
      </Event>
    </EventStream>"""
    mpd = MPD_TEMPLATE.format(
        periods=_period("p0", "PT0S", event_streams=event_stream, segment_r=1)
    )

    _, markers, _ = extract_dash(mpd, "https://cdn.example.com/live/manifest.mpd")

    assert len(markers) == 1
    assert markers[0].source == "eventstream-bin"
    assert markers[0].pts_time_ticks == 180_000
    assert markers[0].declared_duration_ticks == 2_700_000
    assert markers[0].splice_command_xml is not None


def test_eventstream_marker_in_second_period_is_offset_by_period_start():
    event_stream = f"""    <EventStream schemeIdUri="urn:scte:scte35:2014:xml+bin" timescale="90000" value="0-out">
      <Event presentationTime="0" id="1207959695">
        <Signal xmlns="urn:scte:scte35:2013:xml">
          <Binary>{REAL_SCTE35_B64}</Binary>
        </Signal>
      </Event>
    </EventStream>"""
    mpd = MPD_TEMPLATE.format(
        periods=(
            _period("p0", "PT0S", segment_r=0)
            + "\n"
            + _period("p1", "PT6S", event_streams=event_stream, segment_r=0)
        )
    )

    _, markers, _ = extract_dash(mpd, "https://cdn.example.com/live/manifest.mpd")

    # Period 1 starts at real tick 540000 (period 0's one 540000-tick
    # segment); the event's own relative_presentation_time is 0.
    assert markers[0].pts_time_ticks == 540_000
