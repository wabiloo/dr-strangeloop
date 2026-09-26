"""Unit test for serve.py's HLS multivariant (master) playlist builder.

Uses fabricated LoopPackage/VideoRendition-like objects (SimpleNamespace)
rather than a real baked package (no GPAC/real media files needed) to keep
this a fast, pure unit test.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from serve import Channel  # noqa: E402


def _rendition(name, codecs, width, height, frame_rate, bandwidth, audio_variant=None):
    return SimpleNamespace(
        name=name,
        video_variant={
            "codecs": codecs,
            "width": width,
            "height": height,
            "frame_rate": frame_rate,
            "bandwidth": bandwidth,
        },
        audio_variant=audio_variant,
        has_audio=audio_variant is not None,
    )


def _fake_package(video_renditions, audio_rendition=None, hls_format="cmaf", hls_ts_mux_audio=True):
    return SimpleNamespace(
        video_renditions=video_renditions,
        audio_rendition=audio_rendition,
        has_audio=audio_rendition is not None,
        hls_format=hls_format,
        hls_ts_mux_audio=hls_ts_mux_audio,
    )


def test_master_playlist_single_rendition_contains_stream_inf_with_correct_attributes():
    rendition = _rendition("1080p", "avc1.640028", 1920, 1080, 25.0, 9_300_000)
    package = _fake_package([rendition])
    channel = Channel.__new__(Channel)
    channel.package = package

    body = channel.build_hls_master_playlist()

    assert body.startswith("#EXTM3U\n")
    assert "#EXT-X-STREAM-INF:" in body
    assert "BANDWIDTH=9300000" in body
    assert 'CODECS="avc1.640028"' in body
    assert "RESOLUTION=1920x1080" in body
    assert "FRAME-RATE=25.000" in body
    assert body.strip().endswith("1080p/live.m3u8")


def test_master_playlist_multi_rendition_has_one_stream_inf_per_rendition():
    r1080 = _rendition("1080p", "avc1.640028", 1920, 1080, 25.0, 9_300_000)
    r720 = _rendition("720p", "avc1.640028", 1280, 720, 25.0, 4_200_000)
    r360 = _rendition("360p", "avc1.640028", 640, 360, 25.0, 780_000)
    package = _fake_package([r1080, r720, r360])
    channel = Channel.__new__(Channel)
    channel.package = package

    body = channel.build_hls_master_playlist()

    assert body.count("#EXT-X-STREAM-INF:") == 3
    assert "1080p/live.m3u8" in body
    assert "720p/live.m3u8" in body
    assert "360p/live.m3u8" in body
    assert "RESOLUTION=1920x1080" in body
    assert "RESOLUTION=1280x720" in body
    assert "RESOLUTION=640x360" in body


def test_master_playlist_includes_audio_group_when_audio_present():
    audio_variant = {"codecs": "mp4a.40.2", "bandwidth": 120_000}
    rendition = _rendition(
        "1080p", "avc1.640028", 1920, 1080, 25.0, 9_300_000, audio_variant=audio_variant
    )
    package = _fake_package([rendition], audio_rendition=rendition)
    channel = Channel.__new__(Channel)
    channel.package = package

    body = channel.build_hls_master_playlist()

    assert '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio"' in body
    assert 'URI="audio.m3u8"' in body
    assert 'AUDIO="audio"' in body
    # BANDWIDTH should be video + audio combined, CODECS should list both.
    assert "BANDWIDTH=9420000" in body
    assert 'CODECS="avc1.640028,mp4a.40.2"' in body


def test_master_playlist_omits_audio_group_when_no_audio():
    rendition = _rendition("1080p", "avc1.640028", 1920, 1080, 25.0, 9_300_000)
    package = _fake_package([rendition])
    channel = Channel.__new__(Channel)
    channel.package = package

    body = channel.build_hls_master_playlist()

    assert "EXT-X-MEDIA" not in body
    assert "AUDIO=" not in body
    assert 'CODECS="avc1.640028"' in body


@pytest.mark.parametrize("mux_audio", [True, False])
def test_ts_master_audio_layout(mux_audio):
    audio_variant = {"codecs": "mp4a.40.2", "bandwidth": 120_000}
    rendition = _rendition("1080p", "avc1.640028", 1920, 1080, 25.0, 9_300_000,
                           audio_variant=audio_variant)
    channel = Channel.__new__(Channel)
    channel.package = _fake_package([rendition], rendition, "ts", mux_audio)
    body = channel.build_hls_master_playlist()
    assert "#EXT-X-VERSION:6" in body
    assert ('URI="audio.m3u8"' in body) is not mux_audio
    assert ('AUDIO="audio"' in body) is not mux_audio
    assert 'CODECS="avc1.640028,mp4a.40.2"' in body


@pytest.mark.parametrize("mux_audio", [True, False])
def test_ts_media_playlist_uses_ts_without_init_or_separate_audio_when_muxed(mux_audio):
    rendition = _rendition("1080p", "avc1.640028", 1920, 1080, 25.0, 9_300_000)
    rendition.segment_boundary_ticks = [0, 360_000]
    audio = _rendition("audio", "", 0, 0, 0, 0)
    audio.audio_segment_boundary_ticks = [0, 360_000]
    package = _fake_package([rendition], audio, "ts", mux_audio)
    package.rendition_by_name = lambda name: rendition
    package.segment_boundary_ticks = [0, 360_000]
    package.segments_per_loop = 2
    package.total_loop_duration_ticks = 720_000
    package.timescale = 90_000
    package.max_segment_duration_seconds_rounded_up = 5
    package.markers = []
    package.cue_tags = "none"
    channel = Channel(package, 0, window_segments=2)
    channel.now_ticks = lambda: 360_000

    video = channel.build_hls_manifest("1080p")
    assert "#EXT-X-MAP" not in video
    assert "#EXT-X-VERSION:6" in video
    assert "seg/0.ts" in video and "seg/1.ts" in video
    if mux_audio:
        with pytest.raises(RuntimeError, match="muxed"):
            channel.build_hls_audio_manifest()
    else:
        audio_playlist = channel.build_hls_audio_manifest()
        assert "/audio/seg/0.ts" in audio_playlist
        assert "#EXT-X-MAP" not in audio_playlist
