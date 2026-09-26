"""Tests for serve.py's `_asset_ids_starting_in_segment` asset-boundary
comment placement logic."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from serve import _asset_ids_starting_in_segment  # noqa: E402

TS = 90_000  # timescale


def _boundary(asset_id: str, start_s: float) -> dict:
    return {"asset_id": asset_id, "start_ticks": round(start_s * TS)}


def test_finds_boundary_whose_start_falls_inside_the_segment():
    boundaries = [_boundary("asset-1", 0.0), _boundary("ad1", 10.0)]

    assert _asset_ids_starting_in_segment(boundaries, 0, round(4.0 * TS)) == ["asset-1"]
    assert _asset_ids_starting_in_segment(boundaries, round(8.0 * TS), round(12.0 * TS)) == ["ad1"]


def test_no_boundary_in_segment_returns_empty():
    boundaries = [_boundary("asset-1", 0.0), _boundary("ad1", 10.0)]

    assert _asset_ids_starting_in_segment(boundaries, round(4.0 * TS), round(8.0 * TS)) == []


def test_boundary_exactly_on_segment_start_is_included():
    boundaries = [_boundary("ad1", 10.0)]

    assert _asset_ids_starting_in_segment(boundaries, round(10.0 * TS), round(14.0 * TS)) == ["ad1"]


def test_boundary_exactly_on_segment_end_belongs_to_the_next_segment():
    boundaries = [_boundary("ad1", 10.0)]

    assert _asset_ids_starting_in_segment(boundaries, round(6.0 * TS), round(10.0 * TS)) == []


def test_multiple_boundaries_in_one_short_segment_all_returned_in_order():
    boundaries = [_boundary("bg", 0.0), _boundary("ad1", 1.0), _boundary("ad2", 2.0)]

    assert _asset_ids_starting_in_segment(boundaries, 0, round(4.0 * TS)) == ["bg", "ad1", "ad2"]
