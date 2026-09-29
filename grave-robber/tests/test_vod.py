"""Tests for vod.py: ingesting a VOD manifest URL as a rendition ladder.

HTTP is replaced by an in-memory fetcher (dict of url -> bytes), so nothing
touches the network; segment bodies are just labelled bytes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grave_robber.cli import main  # noqa: E402
from grave_robber.manifest_writer import build_ladder_segment_list_manifest  # noqa: E402
from grave_robber.models import TimingSegment  # noqa: E402
from grave_robber.vod import VodIngestError, ingest_url, select_variants  # noqa: E402

BASE = "https://cdn.example/vod/"

MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="en",DEFAULT=YES,URI="audio/index.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.64001e,mp4a.40.2",AUDIO="aud"
360/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2",AUDIO="aud"
720/index.m3u8
"""


def _media_playlist(prefix: str, durations=(4.0, 4.0, 2.0), *, endlist=True, discontinuity_at=None, extra="") -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-TARGETDURATION:4", extra] if extra else [
        "#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-TARGETDURATION:4"]
    for i, d in enumerate(durations):
        if i == discontinuity_at:
            lines.append("#EXT-X-DISCONTINUITY")
        lines += [f"#EXTINF:{d},", f"{prefix}{i}.ts"]
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _hls_site(**overrides) -> dict[str, bytes]:
    site = {
        BASE + "master.m3u8": MASTER,
        BASE + "720/index.m3u8": _media_playlist("v720_"),
        BASE + "360/index.m3u8": _media_playlist("v360_"),
        BASE + "audio/index.m3u8": _media_playlist("a_"),
    }
    site.update(overrides)
    out = {k: v.encode() for k, v in site.items()}
    for prefix, folder in (("v720_", "720/"), ("v360_", "360/"), ("a_", "audio/")):
        for i in range(3):
            out.setdefault(f"{BASE}{folder}{prefix}{i}.ts", f"{prefix}{i}".encode())
    return out


class FakeSite:
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.requests: list[tuple[str, tuple[int, int] | None]] = []

    def __call__(self, url, byte_range=None):
        self.requests.append((url, byte_range))
        if url not in self.files:
            raise VodIngestError(f"GET {url} failed: 404")
        body = self.files[url]
        if byte_range is not None:
            offset, length = byte_range
            body = body[offset:offset + length]
        return body


def _ingest(tmp_path, files, url="master.m3u8", **kw):
    return ingest_url(BASE + url, tmp_path / "out", fetch=FakeSite(files), **kw)


def _read(path) -> bytes:
    return Path(path).read_bytes()


# ── HLS ladder ───────────────────────────────────────────────────────────


def test_hls_multivariant_becomes_a_ladder_with_shared_timeline_and_per_rendition_media(tmp_path):
    manifest = _ingest(tmp_path, _hls_site())

    assert [r["name"] for r in manifest["renditions"]] == ["720p", "360p"]  # best bandwidth first
    assert [e["duration_ticks"] for e in manifest["segments"]] == [360_000, 360_000, 180_000]
    assert all("media_file" not in e for e in manifest["segments"])
    by_name = {r["name"]: r for r in manifest["renditions"]}
    assert by_name["720p"]["variant"] == {
        "codecs": "avc1.64001f,mp4a.40.2", "bandwidth": 3_000_000, "resolution": "1280x720"}
    assert [_read(f) for f in by_name["720p"]["media_files"]] == [b"v720_0", b"v720_1", b"v720_2"]
    assert [_read(f) for f in by_name["360p"]["media_files"]] == [b"v360_0", b"v360_1", b"v360_2"]
    # Separate audio playlist declared by the reference variant's audio group.
    assert manifest["audio"] == {"separate": True}
    assert [_read(e["audio_media_file"]) for e in manifest["segments"]] == [b"a_0", b"a_1", b"a_2"]
    assert json.loads((tmp_path / "out" / "manifest.json").read_text()) == manifest


def test_renditions_selector_keeps_only_the_requested_ones(tmp_path):
    manifest = _ingest(tmp_path, _hls_site(), renditions="360")

    # A ladder of one degenerates to the classic single-rendition shape.
    assert "renditions" not in manifest
    assert manifest["variant"]["resolution"] == "640x360"
    assert [_read(e["media_file"]) for e in manifest["segments"]] == [b"v360_0", b"v360_1", b"v360_2"]


def test_select_variants_forms():
    variants = [
        {"resolution": "640x360", "bandwidth": 800_000},
        {"resolution": "1280x720", "bandwidth": 3_000_000},
        {"resolution": "1920x1080", "bandwidth": 6_000_000},
    ]
    heights = lambda sel: [v["resolution"].split("x")[1] for v in select_variants(variants, sel)]  # noqa: E731
    assert heights(None) == heights("all") == ["1080", "720", "360"]
    assert heights("best") == ["1080"]
    assert heights("360p,1080") == ["1080", "360"]
    assert heights("#2") == ["720"]
    with pytest.raises(VodIngestError, match="matches none"):
        select_variants(variants, "480")


def test_no_audio_flag_skips_the_audio_playlist(tmp_path):
    site = FakeSite(_hls_site())
    manifest = ingest_url(BASE + "master.m3u8", tmp_path / "out", fetch=site, audio=False)

    assert "audio" not in manifest
    assert not any("audio/" in url for url, _ in site.requests)


def test_media_playlist_url_is_a_single_rendition(tmp_path):
    manifest = _ingest(tmp_path, _hls_site(), url="720/index.m3u8")

    assert "renditions" not in manifest
    assert [_read(e["media_file"]) for e in manifest["segments"]] == [b"v720_0", b"v720_1", b"v720_2"]


def test_discontinuities_and_alignment_tolerance(tmp_path):
    site = _hls_site(**{
        BASE + "720/index.m3u8": _media_playlist("v720_", discontinuity_at=2),
        # Container rounding: a few ms off is still aligned.
        BASE + "360/index.m3u8": _media_playlist("v360_", durations=(4.004, 3.996, 2.0), discontinuity_at=2),
    })

    manifest = _ingest(tmp_path, site)

    assert [e["asset_boundary"] for e in manifest["segments"]] == [False, False, True]
    assert [e["duration_ticks"] for e in manifest["segments"]] == [360_000, 360_000, 180_000]  # reference's


@pytest.mark.parametrize(
    "playlist, match",
    [
        (_media_playlist("v360_", durations=(4.0, 4.0)), "segment-aligned"),
        (_media_playlist("v360_", durations=(4.0, 5.0, 2.0)), "not segment-aligned"),
        (_media_playlist("v360_", discontinuity_at=1), "discontinuity mismatch"),
    ],
)
def test_misaligned_ladder_is_rejected(tmp_path, playlist, match):
    with pytest.raises(VodIngestError, match=match):
        _ingest(tmp_path, _hls_site(**{BASE + "360/index.m3u8": playlist}))


def test_live_playlist_is_rejected(tmp_path):
    with pytest.raises(VodIngestError, match="no #EXT-X-ENDLIST"):
        _ingest(tmp_path, _hls_site(**{BASE + "720/index.m3u8": _media_playlist("v720_", endlist=False)}))


def test_failed_download_fails_unless_missing_segments_are_allowed(tmp_path):
    files = _hls_site()
    del files[BASE + "360/v360_1.ts"]

    with pytest.raises(VodIngestError, match="v360_1"):
        _ingest(tmp_path, files)

    manifest = _ingest(tmp_path, files, allow_missing_segments=True)
    by_name = {r["name"]: r for r in manifest["renditions"]}
    assert by_name["360p"]["media_files"][1] is None
    assert all(f is not None for f in by_name["720p"]["media_files"])


def test_byte_range_segments_and_shared_init_are_fetched_once_with_ranges(tmp_path):
    playlist = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:4
#EXT-X-MAP:URI="one.mp4",BYTERANGE="4@0"
#EXTINF:4.0,
#EXT-X-BYTERANGE:3@4
one.mp4
#EXTINF:4.0,
#EXT-X-BYTERANGE:3
one.mp4
#EXT-X-ENDLIST
"""
    site = FakeSite({BASE + "p.m3u8": playlist.encode(), BASE + "one.mp4": b"INITaaabbb"})

    manifest = ingest_url(BASE + "p.m3u8", tmp_path / "out", fetch=site)

    assert [_read(e["media_file"]) for e in manifest["segments"]] == [b"INITaaa", b"INITbbb"]
    assert site.requests.count((BASE + "one.mp4", (0, 4))) == 1  # init fetched once
    assert (BASE + "one.mp4", (4, 3)) in site.requests and (BASE + "one.mp4", (7, 3)) in site.requests


# ── DASH ladder ──────────────────────────────────────────────────────────

MPD = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="{type}" mediaPresentationDuration="PT8S" minBufferTime="PT2S"
     profiles="urn:mpeg:dash:profile:isoff-live:2011">
  <Period>
    <AdaptationSet mimeType="video/mp4" segmentAlignment="true">
      <SegmentTemplate timescale="1000" duration="4000" initialization="$RepresentationID$/init.mp4"
                       media="$RepresentationID$/seg_$Number$.m4s" startNumber="1"/>
      <Representation id="lo" bandwidth="800000" width="640" height="360" codecs="avc1.64001e" frameRate="25"/>
      <Representation id="hi" bandwidth="3000000" width="1280" height="720" codecs="avc1.64001f" frameRate="25"/>
    </AdaptationSet>
  </Period>
</MPD>
"""


def _dash_site(mpd_type="static") -> dict[str, bytes]:
    files = {BASE + "stream.mpd": MPD.format(type=mpd_type).encode()}
    for rep in ("lo", "hi"):
        files[f"{BASE}{rep}/init.mp4"] = f"init_{rep}|".encode()
        for n in (1, 2):
            files[f"{BASE}{rep}/seg_{n}.m4s"] = f"{rep}{n}".encode()
    return files


def test_dash_representations_become_a_ladder(tmp_path):
    manifest = _ingest(tmp_path, _dash_site(), url="stream.mpd")

    assert [r["name"] for r in manifest["renditions"]] == ["720p", "360p"]
    assert [e["duration_ticks"] for e in manifest["segments"]] == [360_000, 360_000]
    by_name = {r["name"]: r for r in manifest["renditions"]}
    assert by_name["720p"]["variant"] == {
        "codecs": "avc1.64001f", "bandwidth": 3_000_000, "resolution": "1280x720", "frame_rate": 25.0}
    assert [_read(f) for f in by_name["720p"]["media_files"]] == [b"init_hi|hi1", b"init_hi|hi2"]
    assert [_read(f) for f in by_name["360p"]["media_files"]] == [b"init_lo|lo1", b"init_lo|lo2"]


def test_dash_representation_selection_and_live_rejection(tmp_path):
    manifest = _ingest(tmp_path, _dash_site(), url="stream.mpd", renditions="best")
    assert "renditions" not in manifest and manifest["variant"]["bandwidth"] == 3_000_000

    with pytest.raises(VodIngestError, match="dynamic"):
        _ingest(tmp_path, _dash_site("dynamic"), url="stream.mpd")


MPD_AUDIO = MPD.replace(
    "</Period>",
    """<AdaptationSet mimeType="audio/mp4" lang="en">
      <SegmentTemplate timescale="48000" duration="{audio_duration}" initialization="aud/init.mp4"
                       media="aud/seg_$Number$.m4s" startNumber="1"/>
      <Representation id="aud" bandwidth="128000" codecs="mp4a.40.2" audioSamplingRate="48000"/>
    </AdaptationSet>
  </Period>""",
)


def _dash_audio_site(audio_duration: int, count: int) -> dict[str, bytes]:
    files = _dash_site()
    files[BASE + "stream.mpd"] = MPD_AUDIO.format(type="static", audio_duration=audio_duration).encode()
    files[BASE + "aud/init.mp4"] = b"init_aud|"
    for n in range(1, count + 1):
        files[f"{BASE}aud/seg_{n}.m4s"] = f"aud{n}".encode()
    return files


def test_dash_separate_audio_adaptation_set_is_imported_and_aligned(tmp_path):
    # 4.01 s audio segments (192480 / 48000) against 4 s video: slightly off, still aligned.
    manifest = _ingest(tmp_path, _dash_audio_site(192_480, 2), url="stream.mpd")

    assert manifest["audio"] == {"separate": True}
    assert [_read(e["audio_media_file"]) for e in manifest["segments"]] == [b"init_aud|aud1", b"init_aud|aud2"]
    assert [e["audio_duration_ticks"] for e in manifest["segments"]] == [360_900, 360_900]


def test_dash_audio_with_no_counterpart_for_a_video_segment_is_a_hole(tmp_path):
    # One 8 s audio segment against two 4 s video segments: nothing starts near the second.
    manifest = _ingest(tmp_path, _dash_audio_site(384_000, 1), url="stream.mpd")

    assert [_read(e["audio_media_file"]) if e["audio_media_file"] else None for e in manifest["segments"]] == [
        b"init_aud|aud1", None]


def test_dash_no_audio_flag_and_muxed_audio(tmp_path):
    manifest = _ingest(tmp_path, _dash_audio_site(192_000, 2), url="stream.mpd", audio=False)
    assert "audio" not in manifest
    assert "audio" not in _ingest(tmp_path, _dash_site(), url="stream.mpd")


# ── manifest writer ──────────────────────────────────────────────────────


def test_ladder_writer_rejects_duplicate_names_and_empty_ladders():
    segments = [TimingSegment(index=0, duration_ticks=100)]
    rendition = {"name": "a", "variant": None, "media_paths": {0: None}}
    with pytest.raises(ValueError, match="Duplicate"):
        build_ladder_segment_list_manifest(segments, [], [], [rendition, dict(rendition)])
    with pytest.raises(ValueError, match="at least one"):
        build_ladder_segment_list_manifest(segments, [], [], [])


# ── CLI ──────────────────────────────────────────────────────────────────


def test_cli_ingest_url(tmp_path, monkeypatch):
    import grave_robber.vod as vod

    site = FakeSite(_hls_site())
    monkeypatch.setattr(vod, "http_fetch", site)
    monkeypatch.setattr(vod.ingest_url, "__kwdefaults__", {**vod.ingest_url.__kwdefaults__, "fetch": site})

    code = main(["ingest-url", BASE + "master.m3u8", "--output", str(tmp_path / "out"), "--renditions", "720"])

    assert code == 0
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert manifest["variant"]["resolution"] == "1280x720"
