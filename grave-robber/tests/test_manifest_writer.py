"""Tests for manifest_writer.py: the segment-list manifest shape
loop-dee-loop's bake.py sparse mode consumes (loop-dee-loop/SCOPE.md
§11.2)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grave_robber.manifest_writer import (  # noqa: E402
    build_segment_list_manifest,
    write_segment_list_manifest,
)
from grave_robber.models import AssetBoundary, TimingSegment  # noqa: E402


def test_build_manifest_basic_shape():
    segments = [
        TimingSegment(index=0, duration_ticks=100, asset_boundary=False),
        TimingSegment(index=1, duration_ticks=200, asset_boundary=True),
    ]
    boundaries = [AssetBoundary(segment_index=1, gap_ticks=0)]
    decoded_markers = [{"source": "daterange", "event_id": "0x1", "pts_time_ticks": 50}]
    media_paths = {0: Path("/out/seg_000000.bin"), 1: None}

    manifest = build_segment_list_manifest(segments, boundaries, decoded_markers, media_paths)

    assert manifest["segments"] == [
        {"index": 0, "duration_ticks": 100, "asset_boundary": False, "media_file": "/out/seg_000000.bin"},
        {"index": 1, "duration_ticks": 200, "asset_boundary": True, "media_file": None},
    ]
    # "source" (grave-robber's own provenance field) is dropped -- not
    # part of loop-dee-loop's markers.json shape.
    assert manifest["markers"] == [{"event_id": "0x1", "pts_time_ticks": 50}]


def test_build_manifest_includes_gap_ticks_only_when_nonzero():
    segments = [
        TimingSegment(index=0, duration_ticks=100),
        TimingSegment(index=1, duration_ticks=100, asset_boundary=True),
    ]
    boundaries = [AssetBoundary(segment_index=1, gap_ticks=45_000)]

    manifest = build_segment_list_manifest(segments, boundaries, [], {})

    assert manifest["segments"][0].get("gap_ticks") is None
    assert manifest["segments"][1]["gap_ticks"] == 45_000


def test_build_manifest_treats_segments_own_asset_boundary_flag_too():
    """A boundary can come either from the AssetBoundary list or the
    segment's own flag (both are populated consistently by the real
    pipeline, but the writer should honor either)."""
    segments = [TimingSegment(index=0, duration_ticks=100, asset_boundary=True)]

    manifest = build_segment_list_manifest(segments, [], [], {})

    assert manifest["segments"][0]["asset_boundary"] is True


def test_write_segment_list_manifest_round_trips(tmp_path):
    manifest = {"segments": [{"index": 0, "duration_ticks": 1, "asset_boundary": False, "media_file": None}], "markers": []}
    path = tmp_path / "out" / "manifest.json"

    write_segment_list_manifest(manifest, path)

    assert json.loads(path.read_text()) == manifest
