"""`--dash-addressing time`: DASH SegmentTemplates use $Time$ (the <S t>
value) instead of $Number$ + startNumber. Every $Time$ URL in the MPD must
resolve to the same bytes as the $Number$ URL of the same segment, in both
timeline modes."""

from __future__ import annotations

import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import Channel, LoopPackage, create_app  # noqa: E402
from test_serve_continuity import _write_continuous_package  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

NS = {"d": "urn:mpeg:dash:schema:mpd:2011"}


def _templates(mpd: str):
    """(SegmentTemplate element, [S t values]) for every Representation."""
    root = ET.fromstring(mpd)
    for template in root.iterfind(".//d:SegmentTemplate", NS):
        yield template, [int(s.get("t")) for s in template.iterfind(".//d:S", NS)]


@pytest.fixture(autouse=True)
def _pinned_now(monkeypatch):
    """Pin 'now' just past the loop wrap so both MPDs show the same window."""
    monkeypatch.setattr(Channel, "now_ticks", lambda self: 45 * 90000)


def _make(tmp_path, *, continuous: bool, addressing: str, now: int = 0):
    tmp_path.mkdir(parents=True, exist_ok=True)
    _write_continuous_package(tmp_path)
    app = create_app(
        tmp_path, epoch_ticks=0, window_segments=4, continuous=continuous, dash_addressing=addressing
    )
    app.config["TESTING"] = True
    return app


@pytest.mark.parametrize("continuous", [False, True])
def test_number_mode_is_unchanged(tmp_path, continuous):
    app = _make(tmp_path, continuous=continuous, addressing="number", now=0)
    mpd = app.test_client().get("/stream.mpd").get_data(as_text=True)
    assert "$Number$" in mpd and "startNumber=" in mpd and "$Time$" not in mpd


@pytest.mark.parametrize("continuous", [False, True])
def test_time_mode_templates_and_urls_serve_the_same_bytes(tmp_path, continuous):
    app = _make(tmp_path, continuous=continuous, addressing="time", now=0)
    client = app.test_client()
    mpd = client.get("/stream.mpd").get_data(as_text=True)
    assert "$Number$" not in mpd and "startNumber" not in mpd
    seen = 0
    for template, times in _templates(mpd):
        media = template.get("media")
        assert "$Time$" in media
        for t in times:
            resp = client.get(media.replace("$Time$", str(t)))
            assert resp.status_code == 200, (media, t)
            seen += 1
    assert seen >= 4


@pytest.mark.parametrize("continuous", [False, True])
def test_time_and_number_urls_return_identical_bytes(tmp_path, continuous):
    num = _make(tmp_path / "n", continuous=continuous, addressing="number").test_client()
    tim = _make(tmp_path / "t", continuous=continuous, addressing="time").test_client()
    num_mpd = num.get("/stream.mpd").get_data(as_text=True)
    time_mpd = tim.get("/stream.mpd").get_data(as_text=True)
    for (nt, n_times), (tt, t_times) in zip(_templates(num_mpd), _templates(time_mpd)):
        assert n_times == t_times
        start = int(nt.get("startNumber"))
        for i, t in enumerate(n_times):
            # Numbers are consecutive within a Period's template.
            a = num.get(nt.get("media").replace("$Number$", str(start + i)))
            b = tim.get(tt.get("media").replace("$Time$", str(t)))
            assert a.status_code == b.status_code == 200
            assert a.data == b.data


@pytest.mark.parametrize("continuous", [False, True])
def test_unknown_time_is_404(tmp_path, continuous):
    client = _make(tmp_path, continuous=continuous, addressing="time", now=0).test_client()
    url = "/1080p/cseg/t/1.m4s" if continuous else "/1080p/seg/t/0/1.m4s"
    assert client.get(url).status_code == 404
    assert client.get("/1080p/seg/t/99/0.m4s").status_code == 404
    assert client.get("/nope/cseg/t/0.m4s").status_code == 404


def test_rejects_unknown_addressing(tmp_path):
    _write_continuous_package(tmp_path)
    with pytest.raises(ValueError, match="dash_addressing"):
        Channel(LoopPackage(tmp_path), epoch_ticks=0, dash_addressing="bogus")


def test_window_json_dash_uris_follow_addressing(tmp_path):
    _write_continuous_package(tmp_path)
    ch = Channel(LoopPackage(tmp_path), epoch_ticks=0, window_segments=4, continuous=True, dash_addressing="time")
    uris = ch.segment_uris(0, 1, None)["dash"]
    assert uris["first_uri"].startswith("1080p/cseg/t/")
    assert uris["last_uri"] != uris["first_uri"]
