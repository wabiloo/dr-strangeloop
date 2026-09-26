"""Filmstrip renderer tests -- hand-built report dicts, no ffmpeg/tsduck."""

from __future__ import annotations

from scte35_filmstrip_html import _fmt_time, render_filmstrip

FPS = 25.0
DUR = 1 / FPS


def _shots(key_time: float, prefix: str) -> list[dict]:
    out = []
    for off in (-3, -2, -1, 0, 1, 2):
        label = f"{off:+d}" if off else "+0"
        out.append({"label": label, "pts_seconds": round(key_time + off * DUR, 6),
                    "is_idr": off == 0, "file": None})
    return out


def _marker(eid: int, code: str, name: str, start: float, stop: float | None) -> dict:
    ev = lambda t: {"pts_ticks": round(t * 90000), "pts_seconds": t, "upid_type": None,
                    "upid_hex": None, "segment_num": None, "segments_expected": None, "flags": {}}
    return {"event_id": eid, "splice_type": "time_signal", "segmentation_type_id": "0x30",
            "type_name": name, "type_code": code, "is_instant": stop is None and code == "CAS",
            "duration_seconds": None if stop is None else stop - start,
            "nesting_depth": 0, "contains": [], "start": ev(start),
            "stop": ev(stop) if stop is not None else None}


def _report(markers, frames) -> dict:
    return {
        "source": {"name": "x.ts", "codec": "h264", "width": 1920, "height": 1080,
                   "fps": FPS, "duration": 60.0},
        "markers": markers, "frames": frames, "checks": [],
        "summary": {"marker_count": len(markers), "checks_total": 0, "checks_failed": 0},
    }


def test_fmt_time_is_hh_mm_ss_mmm():
    assert _fmt_time(20.0) == "00:00:20.000"
    assert _fmt_time(3723.456) == "01:02:03.456"


def test_same_type_spans_share_one_row_and_stop_tag_moves_to_previous_frame():
    markers = [
        _marker(1, "PAD", "Provider Advertisement", 10.0, 20.0),
        _marker(2, "PAD", "Provider Advertisement", 20.0, 30.0),
    ]
    frames = {
        "evt1_start": _shots(10.0, "a"), "evt1_stop": _shots(20.0, "b"),
        "evt2_start": _shots(20.0, "c"), "evt2_stop": _shots(30.0, "d"),
    }
    html = render_filmstrip(_report(markers, frames))
    assert "Inspector Krogh" in html
    assert "grid-row:1;" in html and "grid-row:2;background" not in html
    assert html.count('title="Provider Advertisement #1 stop"') + html.count('title="Provider Advertisement #2 stop"') == 2
    assert html.count('title="Provider Advertisement #1 start"') + html.count('title="Provider Advertisement #2 start"') == 2
    assert "depth" not in html.lower()


def test_endcap_frames_and_ellipsis():
    markers = [_marker(1, "BRK", "Break", 10.0, 20.0)]
    frames = {
        "evt1_start": _shots(10.0, "a"), "evt1_stop": _shots(20.0, "b"),
        "asset_start": [{"label": str(i), "pts_seconds": round(i * DUR, 6), "is_idr": i == 0, "file": None} for i in range(3)],
        "asset_end": [{"label": str(i), "pts_seconds": round(59.92 + i * DUR, 6), "is_idr": False, "file": None} for i in range(3)],
    }
    html = render_filmstrip(_report(markers, frames))
    assert "00:00:00.000" in html
    assert "00:00:59.960" in html
    assert html.count('<span class="ell-dots">') == 3


def test_asset_bars_join_frames_and_durations():
    markers = [_marker(1, "BRK", "Break", 10.0, 20.0)]
    frames = {"evt1_start": _shots(10.0, "a"), "evt1_stop": _shots(20.0, "b"),
              "trans1": _shots(15.0, "t")}
    report = _report(markers, frames)
    report["timeline"] = {
        "assets": [
            {"asset_id": "a1", "file": "a1.mp4", "start": 0.0, "end": 10.0},
            {"asset_id": "a2", "file": "a2.mp4", "start": 10.0, "end": 15.0},
            {"asset_id": "a3", "file": "a3.mp4", "start": 15.0, "end": 20.0},
        ],
        "transitions": [{"index": 1, "time": 15.0, "from_asset": "a2", "from_file": "a2.mp4",
                         "to_asset": "a3", "to_file": "a3.mp4"}],
    }
    html = render_filmstrip(report)
    assert html.count('class="span-bar asset') >= 2
    assert "a2 · 00:00:05.000" in html
    assert "Break #1 · 00:00:10.000" in html
    # only 2 frames before / 1 after the +0 frame are kept for a marker-less join
    assert "00:00:14.920" in html and "00:00:15.040" in html
    assert "00:00:14.880" not in html and "00:00:15.080" not in html
