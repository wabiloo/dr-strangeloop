"""Generator (ffmpeg lavfi) assets in Igor: marker/timeline preview must work
without rendering anything, and playlist durations must count generators."""

from __future__ import annotations

from igor.integrations import franken_ts as ft

PLAYLIST = {
    "output": {"file": "out.ts", "resolution": "640x360", "framerate": 25},
    "assets": [
        {"id": "content", "generator": {"duration": "6s", "timecode": True}},
        {"id": "ad1", "generator": {"duration": "4s", "preset": "color", "color": "red"}},
    ],
    "markers": [{"event_id": 1, "type": "ad", "splice_type": "splice_insert", "assets": ["ad1"]}],
}


def test_resolve_markers_preview_needs_no_render():
    result = ft.resolve_markers_preview("unused", PLAYLIST)
    assert result["asset_durations"] == {"content": 6.0, "ad1": 4.0}
    start = next(m for m in result["markers"] if m["event_id"] == 1)
    assert start["start_seconds"] == 6.0


def test_file_and_generator_are_exclusive():
    import pytest

    bad = {**PLAYLIST, "assets": [{"id": "x", "file": "a.mp4", "generator": {"duration": 3}}]}
    with pytest.raises(Exception):
        ft.resolve_markers_preview("unused", bad)
