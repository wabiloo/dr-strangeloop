"""Tests for serve.py's grave-robber/SCOPE.md §6.1 (internal asset-boundary
discontinuities) and §6.2 (declared-vs-serving position split) extensions,
plus the §11.3 segment-byte 404 guard.

Pure-function tests use the naive/reference formulas as ground truth
(mirroring test_loop_math.py's own style of testing against independently
computed expected values, not just re-running the implementation). The
HLS/DASH manifest tests use fabricated SimpleNamespace packages, same
convention as tests/test_hls_master_playlist.py. The 404-guard test builds
a real, tiny on-disk sparse loop package (fake byte content -- no real
GPAC/ffmpeg needed, VideoRendition only checks file presence) and drives it
through Flask's test client.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
import types
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from serve import (  # noqa: E402
    LoopPackage,
    Channel,
    LoopPackage,
    compute_asset_boundary_set,
    compute_declared_offset_ticks_by_local_index,
    compute_discontinuity_sequence,
    create_app,
)


# ── compute_asset_boundary_set ───────────────────────────────────────────


def test_asset_boundary_set_always_includes_zero():
    assert compute_asset_boundary_set([]) == {0}
    assert compute_asset_boundary_set([3, 7]) == {0, 3, 7}
    assert compute_asset_boundary_set([0, 3]) == {0, 3}


# ── compute_discontinuity_sequence ───────────────────────────────────────


def test_discontinuity_sequence_reduces_to_plain_loop_number_with_no_internal_boundaries():
    """SCOPE.md §6.1: with boundaries == {0} (no internal asset joins --
    the normal franken-ts-authored case), this must be byte-identical to
    the original pre-grave-robber formula, `global_index //
    segments_per_loop`."""
    segments_per_loop = 5
    boundaries = {0}
    for global_index in range(0, 5 * segments_per_loop):
        expected = global_index // segments_per_loop
        assert compute_discontinuity_sequence(global_index, segments_per_loop, boundaries) == expected


def test_discontinuity_sequence_counts_internal_boundaries_too():
    """2 spans per loop (boundaries at local index 0 and 2, segments_per_loop=4):
    loop 0: global 0,1 -> seq 0; global 2,3 -> seq 1
    loop 1: global 4,5 -> seq 2; global 6,7 -> seq 3
    """
    segments_per_loop = 4
    boundaries = {0, 2}
    expected_by_global_index = {0: 0, 1: 0, 2: 1, 3: 1, 4: 2, 5: 2, 6: 3, 7: 3}
    for global_index, expected in expected_by_global_index.items():
        assert compute_discontinuity_sequence(global_index, segments_per_loop, boundaries) == expected


def test_discontinuity_sequence_matches_brute_force_reference():
    """Ground truth: literally count boundary crossings from index 0 up to
    (and including) global_index, independently of the closed-form
    implementation under test."""

    def _brute_force(global_index, segments_per_loop, boundaries):
        count = 0
        for g in range(global_index + 1):
            if g % segments_per_loop in boundaries:
                count += 1
        return count - 1

    segments_per_loop = 6
    boundaries = {0, 1, 4}
    for global_index in range(0, 3 * segments_per_loop):
        assert compute_discontinuity_sequence(
            global_index, segments_per_loop, boundaries
        ) == _brute_force(global_index, segments_per_loop, boundaries)


# ── compute_declared_offset_ticks_by_local_index ─────────────────────────


def test_declared_offset_all_zero_with_no_gap_ticks():
    offsets = compute_declared_offset_ticks_by_local_index(4, {0, 2}, {})
    assert offsets == [0, 0, 0, 0]


def test_declared_offset_accumulates_gap_at_boundary():
    # A +45000-tick gap declared at internal boundary index 2.
    offsets = compute_declared_offset_ticks_by_local_index(4, {0, 2}, {2: 45_000})
    assert offsets == [0, 0, 45_000, 45_000]


def test_declared_offset_supports_negative_overlap():
    offsets = compute_declared_offset_ticks_by_local_index(4, {0, 2}, {2: -9_000})
    assert offsets == [0, 0, -9_000, -9_000]


def test_declared_offset_accumulates_across_multiple_boundaries():
    offsets = compute_declared_offset_ticks_by_local_index(6, {0, 2, 4}, {2: 1_000, 4: -300})
    assert offsets == [0, 0, 1_000, 1_000, 700, 700]


def test_declared_offset_ignores_gap_ticks_for_non_boundary_index():
    # A stray entry for an index that isn't actually a declared boundary
    # must never contribute -- only entries in `boundaries` matter.
    offsets = compute_declared_offset_ticks_by_local_index(4, {0}, {2: 999})
    assert offsets == [0, 0, 0, 0]


# ── HLS media playlist: internal discontinuity + declared PDT ───────────


def _sparse_fake_package(boundaries, gap_ticks_by_index, segment_boundary_ticks, total_loop_duration_ticks):
    segments_per_loop = len(segment_boundary_ticks)
    rendition = SimpleNamespace(
        name="archive",
        video_variant={"codecs": "avc1.640028", "width": 1920, "height": 1080, "frame_rate": 25.0, "bandwidth": 1_000_000},
        audio_variant=None,
        has_audio=False,
        sparse=True,
        self_initializing=True,
        shared_init=False,
        segment_boundary_ticks=segment_boundary_ticks,
    )
    boundary_set = compute_asset_boundary_set(boundaries)
    package = SimpleNamespace(
        video_renditions=[rendition],
        audio_rendition=None,
        has_audio=False,
        hls_format="cmaf",
        hls_ts_mux_audio=True,
        rendition_by_name=lambda name: rendition,
        segment_boundary_ticks=segment_boundary_ticks,
        segments_per_loop=segments_per_loop,
        total_loop_duration_ticks=total_loop_duration_ticks,
        timescale=90_000,
        max_segment_duration_seconds_rounded_up=5,
        markers=[],
        cue_tags="none",
        daterange_mode="shared",
        increment_event_ids=False,
        daterange_id_format=None,
        boundaries=boundary_set,
        declared_offset_ticks_by_local_index=compute_declared_offset_ticks_by_local_index(
            segments_per_loop, boundary_set, gap_ticks_by_index
        ),
    )
    package.audio_muxed_in_video = False
    package.video_playlist_name = types.MethodType(LoopPackage.video_playlist_name, package)
    return package


def test_hls_media_playlist_emits_discontinuity_at_internal_asset_boundary():
    # 4 segments/loop, 90000 ticks each (=1s), internal boundary at index 2.
    boundary_ticks = [0, 90_000, 180_000, 270_000]
    package = _sparse_fake_package(
        boundaries=[2], gap_ticks_by_index={}, segment_boundary_ticks=boundary_ticks,
        total_loop_duration_ticks=360_000,
    )
    channel = Channel(package, epoch_ticks=0, window_segments=4)
    channel.now_ticks = lambda: 359_000  # inside segment index 3, first loop

    body = channel.build_hls_manifest("archive")

    # Window covers indices 0..3 of loop 0; discontinuity fires once,
    # right before segment index 2 (not before index 0, which is covered
    # by the header's DISCONTINUITY-SEQUENCE instead).
    assert body.count("#EXT-X-DISCONTINUITY\n") == 1
    lines = body.splitlines()
    disc_pos = lines.index("#EXT-X-DISCONTINUITY")
    assert lines[disc_pos + 3] == "archive/seg/2.m4s"


def test_hls_media_playlist_declared_pdt_includes_gap_offset():
    boundary_ticks = [0, 90_000, 180_000, 270_000]
    package = _sparse_fake_package(
        boundaries=[2], gap_ticks_by_index={2: 45_000}, segment_boundary_ticks=boundary_ticks,
        total_loop_duration_ticks=360_000,
    )
    channel = Channel(package, epoch_ticks=0, window_segments=1)
    # Land exactly on segment index 2 (starts at real tick 180_000).
    channel.now_ticks = lambda: 180_000

    body = channel.build_hls_manifest("archive")

    # Declared PDT = epoch(0) + loop(0) + (segment_start_ticks=180000 +
    # gap 45000) = 225000 ticks = 2.5s after epoch (1970-01-01T00:00:00Z).
    assert "#EXT-X-PROGRAM-DATE-TIME:1970-01-01T00:00:02.500Z" in body


def test_hls_media_playlist_discontinuity_sequence_header_counts_internal_boundaries():
    boundary_ticks = [0, 90_000, 180_000, 270_000]
    package = _sparse_fake_package(
        boundaries=[2], gap_ticks_by_index={}, segment_boundary_ticks=boundary_ticks,
        total_loop_duration_ticks=360_000,
    )
    # Window of 1 segment, positioned at the start of loop 1's span 0
    # (global index 4): 2 spans/loop * 1 completed loop = discontinuity
    # sequence 2.
    channel = Channel(package, epoch_ticks=0, window_segments=1)
    channel.now_ticks = lambda: 360_000  # start of loop 1

    body = channel.build_hls_manifest("archive")

    assert "#EXT-X-DISCONTINUITY-SEQUENCE:2" in body


def test_hls_media_playlist_no_internal_boundaries_matches_original_behavior():
    """Regression guard: default (no asset_boundaries beyond 0, no
    gap_ticks) reduces to today's exact plain-loop-wrap behavior."""
    boundary_ticks = [0, 90_000]
    package = _sparse_fake_package(
        boundaries=[], gap_ticks_by_index={}, segment_boundary_ticks=boundary_ticks,
        total_loop_duration_ticks=180_000,
    )
    channel = Channel(package, epoch_ticks=0, window_segments=3)
    channel.now_ticks = lambda: 270_000  # global index 3 (loop 1, local 1)

    body = channel.build_hls_manifest("archive")

    assert body.count("#EXT-X-DISCONTINUITY\n") == 1  # only the loop-1 wrap
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:0" in body


def test_per_span_init_is_declared_after_every_discontinuity():
    """Proper CMAF from an archive: one init per output span -- HLS repeats
    #EXT-X-MAP after each #EXT-X-DISCONTINUITY (loop wrap AND asset boundary),
    DASH names a different `initialization` per Period."""
    boundary_ticks = [0, 90_000, 180_000, 270_000]
    package = _sparse_fake_package(
        boundaries=[2], gap_ticks_by_index={}, segment_boundary_ticks=boundary_ticks,
        total_loop_duration_ticks=360_000,
    )
    rendition = package.video_renditions[0]
    rendition.self_initializing = False
    rendition.shared_init = True
    channel = Channel(package, epoch_ticks=0, window_segments=4)
    channel.now_ticks = lambda: 360_000 + 90_000  # loop 1, local segment 1

    lines = channel.build_hls_manifest("archive").splitlines()

    maps = [l for l in lines if l.startswith("#EXT-X-MAP")]
    assert maps[0] == '#EXT-X-MAP:URI="archive/init_1.mp4"'  # window opens in span 1 (segments 2..3 of loop 0)
    for i, line in enumerate(lines):
        if line == "#EXT-X-DISCONTINUITY":
            assert lines[i + 1].startswith("#EXT-X-MAP")
    assert 'URI="archive/init_0.mp4"' in "\n".join(maps)

    mpd = channel.build_dash_manifest()
    assert 'initialization="archive/init_0.mp4"' in mpd and 'initialization="archive/init_1.mp4"' in mpd


# ── DASH manifest: internal Period split + declared Period start= ───────


def test_dash_manifest_splits_into_a_new_period_at_internal_asset_boundary():
    boundary_ticks = [0, 90_000, 180_000, 270_000]
    package = _sparse_fake_package(
        boundaries=[2], gap_ticks_by_index={2: 45_000}, segment_boundary_ticks=boundary_ticks,
        total_loop_duration_ticks=360_000,
    )
    # window_segments == the current open span's own length (2), so no
    # padding from the previous span is needed -- exactly one Period.
    channel = Channel(package, epoch_ticks=0, window_segments=2)
    channel.now_ticks = lambda: 359_000  # live edge at segment 3, loop 0

    mpd = channel.build_dash_manifest()

    assert mpd.count("<Period ") == 1
    assert 'id="loop0-2"' in mpd
    # Declared start = 0*360000 + segment_boundary_ticks[2]=180000 +
    # gap 45000 = 225000 ticks = 2.5s.
    assert 'start="PT2.5S"' in mpd


def test_dash_manifest_no_internal_boundaries_matches_original_period_id():
    boundary_ticks = [0, 90_000]
    package = _sparse_fake_package(
        boundaries=[], gap_ticks_by_index={}, segment_boundary_ticks=boundary_ticks,
        total_loop_duration_ticks=180_000,
    )
    channel = Channel(package, epoch_ticks=0, window_segments=2)
    channel.now_ticks = lambda: 90_000  # loop 0, local index 1

    mpd = channel.build_dash_manifest()

    assert 'id="loop0"' in mpd
    assert 'start="PT0.0S"' in mpd


def test_dash_manifest_pads_from_previous_span_when_window_exceeds_current_span():
    boundary_ticks = [0, 90_000, 180_000, 270_000]
    package = _sparse_fake_package(
        boundaries=[2], gap_ticks_by_index={2: 45_000}, segment_boundary_ticks=boundary_ticks,
        total_loop_duration_ticks=360_000,
    )
    # window bigger than the current open span's own length (2) -> pulls
    # the tail of the previous span (loop0 span0) in as a second Period.
    channel = Channel(package, epoch_ticks=0, window_segments=4)
    channel.now_ticks = lambda: 359_000  # live edge at segment 3, loop 0

    mpd = channel.build_dash_manifest()

    assert mpd.count("<Period ") == 2
    assert 'id="loop0-0"' in mpd  # span0, no declared offset (0 < boundary 2)
    assert 'id="loop0-2"' in mpd  # span1, carries the declared gap offset


# ── §11.3: segment byte-serving 404 guard, real on-disk sparse package ──


def _write_sparse_package(package_dir: Path) -> None:
    segments_dir = package_dir / "segments" / "archive"
    segments_dir.mkdir(parents=True)
    (segments_dir / "seg_000000.m4s").write_bytes(b"seg0")
    # index 1 intentionally has no file (media_file was null at bake time).
    (segments_dir / "seg_000002.m4s").write_bytes(b"seg2")

    descriptor = {
        "version": 2,
        "timescale": 90_000,
        "total_loop_duration_ticks": 270_000,
        "segment_duration_seconds": 1.0,
        "hls_format": "cmaf",
        "hls_ts_mux_audio": True,
        "daterange_mode": "shared",
        "cue_tags": "none",
        "increment_event_ids": False,
        "daterange_id_format": None,
        "markers": [],
        "asset_boundaries": [],
        "video_renditions": [
            {
                "name": "archive",
                "sparse": True,
                "video_track_id": None,
                "audio_track_id": None,
                "total_loop_duration_ticks": 270_000,
                "segment_boundary_ticks": [0, 90_000, 180_000],
                "segment_present": [True, False, True],
                "audio_segment_boundary_ticks": None,
                "video_variant": {
                    "codecs": "avc1.640028", "width": 1920, "height": 1080,
                    "frame_rate": 25.0, "bandwidth": 1_000_000,
                },
                "audio_variant": None,
            }
        ],
        "source_input": "manifest.json",
        "source_markers_json": "manifest.json",
    }
    (package_dir / "loop_descriptor.json").write_text(json.dumps(descriptor))


def test_loop_package_loads_sparse_rendition_with_holes(tmp_path):
    _write_sparse_package(tmp_path)

    package = LoopPackage(tmp_path)
    rendition = package.rendition_by_name("archive")

    assert rendition.sparse is True
    assert rendition.segment_path_for_index(0) is not None
    assert rendition.segment_path_for_index(1) is None
    assert rendition.segment_path_for_index(2) is not None


def test_segment_route_404s_on_missing_media_but_serves_present_ones(tmp_path):
    _write_sparse_package(tmp_path)
    app = create_app(tmp_path, epoch_ticks=0, window_segments=2)
    client = app.test_client()

    ok_response = client.get("/archive/seg/0.m4s")
    missing_response = client.get("/archive/seg/1.m4s")
    also_ok_response = client.get("/archive/seg/2.m4s")

    assert ok_response.status_code == 200
    assert missing_response.status_code == 404
    assert also_ok_response.status_code == 200


def test_manifest_still_advertises_every_segment_despite_holes(tmp_path):
    """SCOPE.md §11.1: manifest generation is never affected by missing
    media -- no #EXT-X-GAP, no dropped segment, ever."""
    _write_sparse_package(tmp_path)
    app = create_app(tmp_path, epoch_ticks=0, window_segments=3)
    client = app.test_client()

    resp = client.get("/video.m3u8")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "seg/0.m4s" in body
    assert "seg/1.m4s" in body  # still advertised even though it 404s
    assert "seg/2.m4s" in body
    assert "#EXT-X-GAP" not in body


def test_init_segment_route_404s_for_self_initializing_rendition(tmp_path):
    _write_sparse_package(tmp_path)
    app = create_app(tmp_path, epoch_ticks=0, window_segments=2)
    client = app.test_client()

    resp = client.get("/archive/init.mp4")

    assert resp.status_code == 404
