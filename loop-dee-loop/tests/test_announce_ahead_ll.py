"""Announcing markers ahead on top of low-latency delivery: the DATERANGE / Event goes where the live edge is, the
parts, preload hint and blocking reload are unaffected, and the plain (non-announcing) manifests are unchanged."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ll_hls import client, lines, playlist_names  # noqa: E402
from test_lowlatency import TS, _write_chunked_package  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture
def pkg(tmp_path):
    """Two 1 s segments (loop 2 s) with a 0x22 break of one segment starting at segment 1 (1 s into the loop)."""
    seg, loop = _write_chunked_package(tmp_path, names=("1080p",))
    path = tmp_path / "loop_descriptor.json"
    descriptor = json.loads(path.read_text())
    descriptor["markers"] = [
        {
            "event_id": "0x00000001",
            "type": "break",
            "splice_type": "time_signal",
            "pts_time_ticks": seg,
            "pts_time_seconds": seg / TS,
            "assets": ["ad-1"],
            "is_out": True,
            "segmentation_type_id": "0x22",
            "is_instant": False,
            "segmentation_duration_ticks": seg,
            "segment_num": 0,
            "segments_expected": 0,
            "splice_command_b64": "AA==",
            "splice_command_b64_narrowed": "AA==",
        }
    ]
    path.write_text(json.dumps(descriptor))
    return tmp_path, seg, loop


def _playlist(path, clock, monkeypatch, **kw) -> list[str]:
    return lines(client(path, clock, monkeypatch, **kw).get(f"/{playlist_names(path)[0]}"))


def test_ll_hls_announces_a_segment_ahead_and_keeps_parts_and_hint(pkg, monkeypatch):
    path, seg, _ = pkg
    clock = [seg // 4]  # 0.25 s into segment 0: the break is one segment away
    plain = _playlist(path, clock, monkeypatch, ll_hls=True)
    ahead = _playlist(path, clock, monkeypatch, ll_hls=True, announce_ahead=1.0)
    assert not any(ln.startswith("#EXT-X-DATERANGE") for ln in plain)
    (daterange,) = [ln for ln in ahead if ln.startswith("#EXT-X-DATERANGE")]
    assert 'START-DATE="1970-01-01T00:00:01.000Z"' in daterange  # the marker's own time
    # nothing else changes: the same parts, the same preload hint, the same rendition reports
    assert [ln for ln in ahead if not ln.startswith("#EXT-X-DATERANGE")] == plain
    assert any(ln.startswith("#EXT-X-PRELOAD-HINT") for ln in ahead)
    # and the tag is still before the PDT of the in-progress segment 0, ahead of its parts
    i = ahead.index(daterange)
    assert ahead[i + 1].startswith("#EXT-X-PROGRAM-DATE-TIME:1970-01-01T00:00:00") and ahead[i + 2].startswith("#EXT-X-PART")


def test_ll_hls_daterange_is_byte_identical_as_the_segment_progresses(pkg, monkeypatch):
    path, seg, _ = pkg
    tags = set()
    for offset in (1, seg // 4, seg // 2, seg - 1, seg, seg + 1, seg + seg // 2):
        clock = [offset]
        tags |= {ln for ln in _playlist(path, clock, monkeypatch, ll_hls=True, announce_ahead=1.0) if ln.startswith("#EXT-X-DATERANGE")}
    assert len(tags) == 1  # one ID, one set of attributes, on every poll (RFC 8216 4.4.5.1)


def test_ll_hls_blocking_reload_returns_the_announced_playlist(pkg, monkeypatch):
    path, seg, _ = pkg
    clock = [1]
    c = client(path, clock, monkeypatch, ll_hls=True, announce_ahead=1.0)
    resp = c.get(f"/{playlist_names(path)[0]}?_HLS_msn=0&_HLS_part=2")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "#EXT-X-DATERANGE" in body and re.search(r"#EXT-X-PART:.*/0\.2\.m4s", body)


def test_ll_dash_event_is_announced_with_the_lead(pkg, monkeypatch):
    path, seg, _ = pkg
    clock = [seg // 4]
    plain = client(path, clock, monkeypatch, ll_dash=True).get("/index.mpd").get_data(as_text=True)
    ahead = client(path, clock, monkeypatch, ll_dash=True, announce_ahead=1.0).get("/index.mpd").get_data(as_text=True)
    assert "<Event" not in plain
    assert re.findall(r'<Event presentationTime="(\d+)"', ahead) == [str(seg)]  # the marker's own time
    assert 'availabilityTimeComplete="false"' in ahead  # still a low-latency MPD


def test_a_query_can_turn_it_on_and_off_per_request(pkg, monkeypatch):
    path, seg, _ = pkg
    clock = [seg // 4]
    name = playlist_names(path)[0]
    c = client(path, clock, monkeypatch, ll_hls=True)
    assert "DATERANGE" not in c.get(f"/{name}").get_data(as_text=True)
    assert "DATERANGE" in c.get(f"/{name}?announce_ahead=1").get_data(as_text=True)
    c = client(path, clock, monkeypatch, ll_hls=True, announce_ahead=1.0)
    assert "DATERANGE" in c.get(f"/{name}").get_data(as_text=True)
    assert "DATERANGE" not in c.get(f"/{name}?announce_ahead=0").get_data(as_text=True)
    assert c.get(f"/{name}?announce_ahead=nope").status_code == 400


def test_the_query_rides_along_to_the_media_playlists(pkg, monkeypatch):
    path, seg, _ = pkg
    c = client(path, [seg // 4], monkeypatch, ll_hls=True)
    master = c.get("/index.m3u8?announce_ahead=1").get_data(as_text=True)
    children = [ln for ln in master.splitlines() if ".m3u8" in ln and not ln.startswith("#EXT-X-MEDIA")]
    assert children and all("announce_ahead=1" in ln for ln in children)
