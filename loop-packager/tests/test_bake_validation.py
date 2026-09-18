"""Tests for bake.py's validation step (SCOPE.md §4.1 step 1, §10 checklist).

These tests exercise validate_markers_against_ts using fabricated
DecodedMarker/markers.json data (not real .ts/GPAC/threefive round-trips,
which require the pinned GPAC build and real franken-ts fixtures -- see
tests/fixtures/README.md for how to add those for full end-to-end coverage).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bake import DecodedMarker, ValidationError, validate_markers_against_ts  # noqa: E402


def _marker(event_id: str, pts_time_ticks: int) -> dict:
    return {
        "event_id": event_id,
        "splice_type": "time_signal",
        "pts_time_ticks": pts_time_ticks,
        "pts_time_seconds": pts_time_ticks / 90_000,
    }


def test_validation_passes_on_exact_match():
    markers = [_marker("0x00000001", 5_400_000), _marker("0x00000002", 9_000_000)]
    decoded = [
        DecodedMarker("0x00000001", 5_400_000, "AAAA"),
        DecodedMarker("0x00000002", 9_000_000, "BBBB"),
    ]

    result = validate_markers_against_ts(markers, decoded)

    assert len(result) == 2
    assert result[0]["splice_command_b64"] in ("AAAA", "BBBB")
    for m in result:
        assert "splice_command_b64" in m


def test_validation_fails_on_missing_event_in_ts():
    markers = [_marker("0x00000001", 5_400_000)]
    decoded: list[DecodedMarker] = []  # nothing actually embedded

    with pytest.raises(ValidationError, match="not present in the .ts"):
        validate_markers_against_ts(markers, decoded)


def test_validation_fails_on_extra_event_in_ts():
    markers: list[dict] = []
    decoded = [DecodedMarker("0x00000001", 5_400_000, "AAAA")]

    with pytest.raises(ValidationError, match="not declared in markers.json"):
        validate_markers_against_ts(markers, decoded)


def test_validation_fails_on_one_tick_pts_mismatch():
    """Even a single-tick PTS drift must be a hard failure -- no tolerance."""
    markers = [_marker("0x00000001", 5_400_000)]
    decoded = [DecodedMarker("0x00000001", 5_400_001, "AAAA")]

    with pytest.raises(ValidationError, match="off by 1 tick"):
        validate_markers_against_ts(markers, decoded)
