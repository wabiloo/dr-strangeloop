"""Tests for serve.py's `_marker_covers_segment` marker/segment overlap logic."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from serve import _marker_covers_segment  # noqa: E402

TS = 90_000  # timescale


def _marker(seg_type_id: str | None, start_s: float, duration_s: float | None) -> dict:
    return {
        "pts_time_ticks": round(start_s * TS),
        "segmentation_type_id": seg_type_id,
        "segmentation_duration_ticks": (
            round(duration_s * TS) if duration_s is not None else None
        ),
    }


def test_ad_start_marker_covers_every_segment_within_its_active_interval():
    """A real CUE-OUT (e.g. 0x30 Provider Ad Start) with a duration must
    keep being signaled across every segment its active interval overlaps,
    not just the one it starts on."""
    marker = _marker("0x30", start_s=10.0, duration_s=15.0)  # active [10, 25)

    assert _marker_covers_segment(marker, round(8 * TS), round(12 * TS))  # overlaps start
    assert _marker_covers_segment(marker, round(20 * TS), round(24 * TS))  # mid-interval
    assert not _marker_covers_segment(marker, round(30 * TS), round(34 * TS))  # after


def test_call_ad_server_instant_marker_only_covers_its_own_segment():
    """0x02 Call Ad Server is even (would satisfy the CUE-OUT rule) and
    carries a `segmentation_duration_ticks`, but it's a standalone/instant
    signal with no matching CUE-IN -- it must be point-in-time only, not
    treated as an open interval spanning that duration."""
    marker = _marker("0x02", start_s=10.0, duration_s=15.0)

    assert _marker_covers_segment(marker, round(8 * TS), round(12 * TS))  # its own segment
    assert not _marker_covers_segment(marker, round(20 * TS), round(24 * TS))  # well inside
    # the bogus "duration" interval -- must NOT be covered


def test_ad_end_marker_is_point_in_time_only():
    marker = _marker("0x31", start_s=25.0, duration_s=15.0)  # CUE-IN (odd type_id)

    assert _marker_covers_segment(marker, round(24 * TS), round(26 * TS))
    assert not _marker_covers_segment(marker, round(10 * TS), round(14 * TS))
