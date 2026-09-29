"""Separate-audio handling: multivariant lookup and alignment to video segments."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grave_robber.audio import align_audio_segments  # noqa: E402
from grave_robber.manifest_writer import build_segment_list_manifest  # noqa: E402
from grave_robber.models import TimingSegment  # noqa: E402
from grave_robber.multivariant import find_audio_playlist  # noqa: E402

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
MULTIVARIANT = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="en",DEFAULT=YES,URI="audio/a.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="mux",NAME="en",DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=1000,CODECS="avc1.4D401F,mp4a.40.2",AUDIO="aud"
v1.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=900,CODECS="avc1.4D401F,mp4a.40.2",AUDIO="mux"
v2.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=800,CODECS="avc1.4D401F"
v3.m3u8
"""


class FakeTrace:
    def get_abr_manifest_urls(self):
        return [SimpleNamespace(url="http://x/m/master.m3u8")]

    def get_entries_for_url(self, url):
        return [SimpleNamespace(content_bytes=MULTIVARIANT.encode())] if url.endswith("master.m3u8") else []


def test_find_audio_playlist_separate_muxed_and_none():
    trace = FakeTrace()
    assert find_audio_playlist(trace, "http://x/m/v1.m3u8")["manifest_url"] == "http://x/m/audio/a.m3u8"
    assert find_audio_playlist(trace, "http://x/m/v2.m3u8") == {"manifest_url": None}  # muxed
    assert find_audio_playlist(trace, "http://x/m/v3.m3u8") is None  # nothing declared


def _seg(i, start_s, dur_ticks=540_000):
    return TimingSegment(i, dur_ticks, start_time=T0 + dt.timedelta(seconds=start_s), source_uri=f"u{i}")


def test_audio_aligned_by_wall_clock_with_holes():
    video = [_seg(0, 0), _seg(1, 6), _seg(2, 12)]
    audio = [_seg(10, 0.02), _seg(11, 12.1)]  # nothing near 6s
    aligned = align_audio_segments(video, audio)
    assert [a.source_uri if a else None for a in aligned] == ["u10", None, "u11"]
    assert [a.index for a in aligned if a] == [0, 2]  # re-indexed to the video segments


def test_alignment_without_wall_clock_needs_equal_counts():
    video = [TimingSegment(i, 540_000) for i in range(2)]
    audio = [TimingSegment(i, 540_000) for i in range(3)]
    with pytest.raises(ValueError):
        align_audio_segments(video, audio)
    assert len(align_audio_segments(video, audio[:2])) == 2


def test_manifest_carries_audio_entries():
    video = [_seg(0, 0), _seg(1, 6)]
    audio = [dataclass_audio for dataclass_audio in align_audio_segments(video, [_seg(5, 0)])]
    manifest = build_segment_list_manifest(
        video, [], [], {0: None, 1: None}, None, audio, {0: Path("/a0.bin")}
    )
    assert manifest["audio"] == {"separate": True}
    assert manifest["segments"][0]["audio_media_file"] == "/a0.bin"
    assert manifest["segments"][1]["audio_media_file"] is None
    assert manifest["segments"][1]["audio_duration_ticks"] == manifest["segments"][1]["duration_ticks"]


def test_align_by_ticks_uses_nearest_start_within_half_a_segment():
    from grave_robber.audio import align_audio_segments_by_ticks

    video = [TimingSegment(index=i, duration_ticks=360_000) for i in range(3)]
    audio = [TimingSegment(index=i, duration_ticks=360_900) for i in range(2)]  # third one missing

    aligned = align_audio_segments_by_ticks(video, audio)

    assert [a.index if a else None for a in aligned] == [0, 1, None]
    assert align_audio_segments_by_ticks(video, []) == [None, None, None]
