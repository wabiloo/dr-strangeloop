"""Unit test for serve.py's HLS multivariant (master) playlist builder.

Uses a fabricated LoopPackage-like object rather than a real baked package
(no GPAC/real media files needed) to keep this a fast, pure unit test.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from serve import Channel  # noqa: E402


def _fake_package(video_variant=None, has_audio=False, audio_variant=None):
    return SimpleNamespace(
        video_variant=video_variant, has_audio=has_audio, audio_variant=audio_variant
    )


def test_master_playlist_contains_stream_inf_with_correct_attributes():
    package = _fake_package(
        video_variant={
            "codecs": "avc1.640028",
            "width": 1920,
            "height": 1080,
            "frame_rate": 25.0,
            "bandwidth": 9_300_000,
        }
    )
    channel = Channel.__new__(Channel)
    channel.package = package

    body = channel.build_hls_master_playlist()

    assert body.startswith("#EXTM3U\n")
    assert "#EXT-X-STREAM-INF:" in body
    assert "BANDWIDTH=9300000" in body
    assert 'CODECS="avc1.640028"' in body
    assert "RESOLUTION=1920x1080" in body
    assert "FRAME-RATE=25.000" in body
    assert body.strip().endswith("live.m3u8")


def test_master_playlist_uses_custom_media_playlist_path():
    package = _fake_package(
        video_variant={
            "codecs": "avc1.640028",
            "width": 1280,
            "height": 720,
            "frame_rate": 29.97,
            "bandwidth": 3_000_000,
        }
    )
    channel = Channel.__new__(Channel)
    channel.package = package

    body = channel.build_hls_master_playlist(media_playlist_path="variant1/live.m3u8")

    assert body.strip().endswith("variant1/live.m3u8")
    assert "RESOLUTION=1280x720" in body


def test_master_playlist_raises_without_video_variant_metadata():
    package = _fake_package(video_variant=None)
    channel = Channel.__new__(Channel)
    channel.package = package

    with pytest.raises(RuntimeError, match="video_variant"):
        channel.build_hls_master_playlist()


def test_master_playlist_includes_audio_group_when_audio_present():
    package = _fake_package(
        video_variant={
            "codecs": "avc1.640028",
            "width": 1920,
            "height": 1080,
            "frame_rate": 25.0,
            "bandwidth": 9_300_000,
        },
        has_audio=True,
        audio_variant={"codecs": "mp4a.40.2", "bandwidth": 120_000},
    )
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
    package = _fake_package(
        video_variant={
            "codecs": "avc1.640028",
            "width": 1920,
            "height": 1080,
            "frame_rate": 25.0,
            "bandwidth": 9_300_000,
        },
        has_audio=False,
    )
    channel = Channel.__new__(Channel)
    channel.package = package

    body = channel.build_hls_master_playlist()

    assert "EXT-X-MEDIA" not in body
    assert "AUDIO=" not in body
    assert 'CODECS="avc1.640028"' in body
