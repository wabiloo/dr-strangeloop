"""`--dash-timeline compact`: implicit @t where contiguous + @r for runs of
equal @d. Expanding a compact SegmentTimeline must give exactly the segments
of the full one."""

from __future__ import annotations

import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import Channel, LoopPackage, create_app, render_segment_timeline  # noqa: E402

NS = {"d": "urn:mpeg:dash:schema:mpd:2011"}


def _expand(xml: str) -> list[tuple[int, int]]:
    """DASH semantics: missing @t = previous end, @r = r extra repeats."""
    out, nxt = [], None
    for m in re.finditer(r"<S ([^>]*)/>", xml):
        a = dict(re.findall(r'(\w+)="(-?\d+)"', m.group(1)))
        t = int(a["t"]) if "t" in a else nxt
        assert t is not None, "first <S> must carry @t"
        for _ in range(int(a.get("r", 0)) + 1):
            out.append((t, int(a["d"])))
            t += int(a["d"])
        nxt = t
    return out


def test_compact_unit_runs_gaps_and_comments():
    items = [
        ("c", "loop 1"),
        ("s", 100, 10), ("s", 110, 10), ("s", 120, 10),  # run of 3
        ("s", 130, 12),  # different d, contiguous: no t
        ("s", 200, 12),  # gap: t again, not merged with previous
        ("c", "asset: a"),
        ("s", 212, 12),  # comment ends the run, t implicit
    ]
    xml = render_segment_timeline(items, compact=True)
    assert xml.splitlines() == [
        "        <!-- loop 1 -->",
        '        <S t="100" d="10" r="2" />',
        '        <S d="12" />',
        '        <S t="200" d="12" />',
        "        <!-- asset: a -->",
        '        <S d="12" />',
    ]
    full = render_segment_timeline(items, compact=False)
    assert _expand(xml) == _expand(full) == [
        (100, 10), (110, 10), (120, 10), (130, 12), (200, 12), (212, 12),
    ]


def test_full_unit_is_one_s_per_segment():
    xml = render_segment_timeline([("s", 0, 5), ("s", 5, 5)], compact=False)
    assert xml.splitlines() == ['        <S t="0" d="5" />', '        <S t="5" d="5" />']


def test_rejects_unknown_timeline(tmp_path):
    pytest.importorskip("cmaf")
    from test_serve_continuity import _write_continuous_package

    _write_continuous_package(tmp_path)
    with pytest.raises(ValueError, match="dash_timeline"):
        Channel(LoopPackage(tmp_path), epoch_ticks=0, dash_timeline="bogus")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
@pytest.mark.parametrize("continuous", [False, True])
@pytest.mark.parametrize("addressing", ["number", "time"])
def test_compact_mpd_expands_to_the_full_timeline(tmp_path, monkeypatch, continuous, addressing):
    from test_serve_continuity import _write_continuous_package

    monkeypatch.setattr(Channel, "now_ticks", lambda self: 45 * 90000)
    mpds = {}
    for style in ("full", "compact"):
        d = tmp_path / style
        d.mkdir()
        _write_continuous_package(d)
        app = create_app(
            d, epoch_ticks=0, window_segments=4, continuous=continuous,
            dash_addressing=addressing, dash_timeline=style,
        )
        mpds[style] = app.test_client().get("/stream.mpd").get_data(as_text=True)
    assert not re.search(r'<S [^>]*\sr="', mpds["full"])

    def timelines(mpd):
        root = ET.fromstring(mpd)
        return [
            ET.tostring(tl, encoding="unicode")
            for tl in root.iterfind(".//d:SegmentTimeline", NS)
        ]

    full, compact = timelines(mpds["full"]), timelines(mpds["compact"])
    assert len(full) == len(compact) > 0
    for f, c in zip(full, compact):
        f, c = re.sub(r' xmlns(:\w+)?="[^"]*"', "", f), re.sub(r' xmlns(:\w+)?="[^"]*"', "", c)
        assert _expand(c) == _expand(f)
    assert any(" r=" in c or " t=" not in c for c in compact), "compact should actually compact something"
