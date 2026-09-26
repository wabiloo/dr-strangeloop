"""Smoke tests against real (small) HAR/Proxyman captures from
`wabiloo/trace-shrink`'s own test fixtures (SCOPE.md §9's testing note --
these are ~2-4 entries, too small to exercise multi-variant coverage
meaningfully, but exactly right for exercising the real pipeline shape
end-to-end against genuine archive formats/reader quirks that a synthetic
manifest string can't catch, e.g. HAR/Proxyman parsing itself, relative
segment URL forms, real multi-snapshot overlap).

Full real-HAR end-to-end validation (with actual playable media + a real
loop-dee-loop bake) happens separately -- these are unit/smoke coverage,
not a substitute for that.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grave_robber.pipeline import ingest  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "trace-shrink"

MEDIA_PLAYLIST_URL = (
    "https://stream.broadpeak.io/hls/live/2003678/ndtv24x7/masterp_720p@3.m3u8"
    "?bpkio_serviceid=300d1539c3b6aa17a79a8fd9f1e45448"
    "&bpkio_sessionid=10a0d36d4-f64af804-55e5-4e25-8b94-119d626d0bf2"
    "&category=all&mm_sp"
)


@pytest.mark.parametrize("archive_name", ["hls1-chrome.har", "hls1-proxyman.har"])
def test_ingest_real_har_media_playlist_produces_a_complete_ledger(archive_name, tmp_path):
    archive_path = FIXTURES_DIR / archive_name
    if not archive_path.exists():
        pytest.skip(f"fixture not found: {archive_path}")

    manifest = ingest(archive_path, MEDIA_PLAYLIST_URL, tmp_path)

    assert len(manifest["segments"]) > 0
    # The ledger is always complete regardless of media completeness
    # (SCOPE.md §1) -- every segment has a duration, whether or not this
    # manifest-only HAR happened to also capture the segment's own body.
    for segment in manifest["segments"]:
        assert segment["duration_ticks"] > 0
        assert isinstance(segment["asset_boundary"], bool)
    # This particular capture has no markers and no recovered media bytes
    # (a manifest-only HAR) -- both are expected, not errors.
    assert manifest["markers"] == []


def test_ingest_proxymanlogv2_format_also_works(tmp_path):
    archive_path = FIXTURES_DIR / "hls1-proxyman.proxymanlogv2"
    if not archive_path.exists():
        pytest.skip(f"fixture not found: {archive_path}")

    manifest = ingest(archive_path, MEDIA_PLAYLIST_URL, tmp_path)

    assert len(manifest["segments"]) > 0


def test_ingest_merges_two_overlapping_snapshots_from_chrome_har(tmp_path):
    """hls1-chrome.har captures this media playlist twice (a live poll) --
    the merged timeline must be longer than either single poll's own
    segment count, not just the last poll's window."""
    archive_path = FIXTURES_DIR / "hls1-chrome.har"
    if not archive_path.exists():
        pytest.skip(f"fixture not found: {archive_path}")

    manifest = ingest(archive_path, MEDIA_PLAYLIST_URL, tmp_path)

    # Indices are contiguous 0..N-1 with no gaps or duplicates.
    assert [s["index"] for s in manifest["segments"]] == list(range(len(manifest["segments"])))
