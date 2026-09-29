"""Tests for extract_hls.py (SCOPE.md §5.1) against synthetic manifest
text -- real HAR captures aren't available in this environment (see
SCOPE.md §9); the trickier timing math is targeted directly here rather
than relying on the (small, ~4-entry) trace-shrink fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grave_robber.extract_hls import extract_hls  # noqa: E402
from grave_robber.models import AssetBoundary  # noqa: E402

REAL_SCTE35_B64 = "/DAvAAAAAAAA///wBQb+dGKQoAAZAhdDVUVJSAAAjn+fCAgAAAAALKChijUCAKnMZ1g="


def test_extracts_segments_with_durations_in_ticks():
    text = """#EXTM3U
#EXT-X-VERSION:6
#EXTINF:6.0,
seg0.ts
#EXTINF:4.5,
seg1.ts
"""
    segments, markers, boundaries = extract_hls(text, "https://cdn.example.com/live/index.m3u8")

    assert [s.duration_ticks for s in segments] == [540_000, 405_000]
    assert [s.index for s in segments] == [0, 1]
    assert markers == []
    assert boundaries == []


def test_resolves_relative_segment_uris_against_manifest_url():
    text = """#EXTM3U
#EXTINF:6.0,
seg0.ts
"""
    segments, _, _ = extract_hls(text, "https://cdn.example.com/live/index.m3u8")

    assert segments[0].source_uri == "https://cdn.example.com/live/seg0.ts"


def test_discontinuity_sets_asset_boundary():
    text = """#EXTM3U
#EXTINF:6.0,
seg0.ts
#EXT-X-DISCONTINUITY
#EXTINF:6.0,
seg1.ts
"""
    segments, _, boundaries = extract_hls(text, "https://cdn.example.com/live/index.m3u8")

    assert [s.asset_boundary for s in segments] == [False, True]
    assert [b.segment_index for b in boundaries] == [1]


def test_daterange_positioned_at_its_own_segment_start():
    text = f"""#EXTM3U
#EXT-X-PROGRAM-DATE-TIME:2024-01-01T00:00:00.000Z
#EXT-X-DATERANGE:ID="b1",START-DATE="2024-01-01T00:00:06.000Z",PLANNED-DURATION=30,SCTE35-OUT={REAL_SCTE35_B64}
#EXTINF:6.0,
seg0.ts
#EXTINF:6.0,
seg1.ts
"""
    _, markers, _ = extract_hls(text, "https://cdn.example.com/live/index.m3u8")

    assert len(markers) == 1
    assert markers[0].source == "daterange"
    assert markers[0].pts_time_ticks == 540_000  # 6s * 90000
    assert markers[0].splice_command_b64 == REAL_SCTE35_B64
    assert markers[0].declared_duration_ticks == 30 * 90_000


def test_daterange_mid_segment_gets_a_sub_segment_offset():
    """A marker declared ahead of its own attached segment's PDT is
    legitimately mid-segment, not snapped to the segment's start."""
    text = f"""#EXTM3U
#EXT-X-PROGRAM-DATE-TIME:2024-01-01T00:00:00.000Z
#EXT-X-DATERANGE:ID="b1",START-DATE="2024-01-01T00:00:02.000Z",SCTE35-OUT={REAL_SCTE35_B64}
#EXTINF:6.0,
seg0.ts
"""
    _, markers, _ = extract_hls(text, "https://cdn.example.com/live/index.m3u8")

    assert markers[0].pts_time_ticks == 2 * 90_000


def test_discontinuity_resets_program_date_time_independently_per_run():
    """SCOPE.md §5.1 gotcha: never assume monotonic wall-clock across a
    discontinuity -- each run gets its own independent time-base."""
    text = f"""#EXTM3U
#EXT-X-PROGRAM-DATE-TIME:2024-01-01T00:00:00.000Z
#EXTINF:6.0,
seg0.ts
#EXT-X-DISCONTINUITY
#EXT-X-PROGRAM-DATE-TIME:2019-06-01T00:00:00.000Z
#EXT-X-DATERANGE:ID="b1",START-DATE="2019-06-01T00:00:03.000Z",SCTE35-OUT={REAL_SCTE35_B64}
#EXTINF:6.0,
seg1.ts
"""
    _, markers, _ = extract_hls(text, "https://cdn.example.com/live/index.m3u8")

    # seg1 starts at cumulative tick 540000 (after seg0's 6s); the marker's
    # own run-local PDT delta (+3s) is added on top of THAT, not affected
    # by the wildly different real-world wall-clock jump across the
    # discontinuity.
    assert markers[0].pts_time_ticks == 540_000 + 3 * 90_000


def test_gap_ticks_computed_across_discontinuity_with_explicit_pdts():
    text = """#EXTM3U
#EXT-X-PROGRAM-DATE-TIME:2024-01-01T00:00:00.000Z
#EXTINF:6.0,
seg0.ts
#EXT-X-DISCONTINUITY
#EXT-X-PROGRAM-DATE-TIME:2024-01-01T00:00:07.000Z
#EXTINF:6.0,
seg1.ts
"""
    _, _, boundaries = extract_hls(text, "https://cdn.example.com/live/index.m3u8")

    # seg0 ends at 00:00:06 (real); seg1 starts at 00:00:07 (declared) ->
    # a 1s (90000-tick) gap.
    assert boundaries == [AssetBoundary(segment_index=1, gap_ticks=90_000)]


def test_gap_ticks_zero_when_no_explicit_pdt_at_boundary():
    text = """#EXTM3U
#EXTINF:6.0,
seg0.ts
#EXT-X-DISCONTINUITY
#EXTINF:6.0,
seg1.ts
"""
    _, _, boundaries = extract_hls(text, "https://cdn.example.com/live/index.m3u8")

    assert boundaries[0].gap_ticks == 0


def test_comcast_style_cue_out_with_scte35_payload():
    text = f"""#EXTM3U
#EXT-OATCLS-SCTE35:{REAL_SCTE35_B64}
#EXT-X-CUE-OUT:30
#EXTINF:6.0,
seg0.ts
#EXT-X-CUE-IN
#EXTINF:6.0,
seg1.ts
"""
    _, markers, _ = extract_hls(text, "https://cdn.example.com/live/index.m3u8")

    # Exactly one marker, at the CUE-OUT point -- CUE-IN carries no
    # separate real SCTE-35 message in this tagging style (see
    # extract_hls.py's handling).
    assert len(markers) == 1
    assert markers[0].source == "cue-out"
    assert markers[0].pts_time_ticks == 0
    assert markers[0].declared_duration_ticks == 30 * 90_000


def test_bare_cue_out_without_payload_produces_no_marker():
    text = """#EXTM3U
#EXT-X-CUE-OUT:30
#EXTINF:6.0,
seg0.ts
"""
    _, markers, _ = extract_hls(text, "https://cdn.example.com/live/index.m3u8")

    assert markers == []


def test_byte_ranges_resolve_omitted_offsets_and_init_range():
    text = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:4
#EXT-X-MAP:URI="v.mp4",BYTERANGE="700@0"
#EXTINF:4.0,
#EXT-X-BYTERANGE:1000@700
v.mp4
#EXTINF:4.0,
#EXT-X-BYTERANGE:2000
v.mp4
#EXT-X-ENDLIST
"""
    segments, _, _ = extract_hls(text, "http://x/a/p.m3u8")

    assert [s.byte_range for s in segments] == [(700, 1000), (1700, 2000)]
    assert all(s.init_byte_range == (0, 700) for s in segments)
