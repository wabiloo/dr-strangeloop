"""Smoke tests for the pure JSON -> HTML renderer -- no ffmpeg/tsduck
involved, just feeding it hand-built report dicts."""

from __future__ import annotations

from scte35_report_html import render_html


def _minimal_report(**overrides) -> dict:
    report = {
        "tool": "krogh",
        "report_version": 1,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "path": "outputs/x.ts", "name": "x.ts", "codec": "h264",
            "profile": "High", "width": 1920, "height": 1080, "fps": 25.0,
            "duration": 60.0, "scte35_table_id": "0xFC", "scte35_pid": None,
        },
        "markers": [],
        "frames": {},
        "checks": [],
        "summary": {"marker_count": 0, "checks_total": 0, "checks_failed": 0},
    }
    report.update(overrides)
    return report


def test_render_html_with_no_markers():
    html = render_html(_minimal_report())
    assert "<html" in html
    assert "x.ts" in html
    assert "No SCTE-35 markers found" in html


def test_render_html_with_a_marker_and_checks():
    report = _minimal_report(
        markers=[{
            "event_id": 1, "splice_type": "time_signal",
            "segmentation_type_id": "0x22", "type_name": "Break", "type_code": "BRK",
            "is_instant": False, "duration_seconds": 30.0, "nesting_depth": 0, "contains": [2],
            "start": {"pts_ticks": 900000, "pts_seconds": 10.0, "upid_type": None,
                      "upid_hex": None, "segment_num": None, "segments_expected": None, "flags": {}},
            "stop": {"pts_ticks": 3600000, "pts_seconds": 40.0, "upid_type": None,
                     "upid_hex": None, "segment_num": None, "segments_expected": None, "flags": {}},
        }],
        frames={"evt1_start": [{"label": "+0", "pts_seconds": 10.0, "is_idr": True, "file": None}]},
        checks=[{"event_id": 1, "boundary": "start", "check": "lands_on_idr", "pass": True, "detail": "ok"}],
        summary={"marker_count": 1, "checks_total": 1, "checks_failed": 0},
    )
    html = render_html(report)
    assert "Event #1" in html
    assert "Break" in html
    assert "BRK" in html
    assert "lands_on_idr" in html
    assert "all checks passed" in html
    assert "depth" not in html
    assert "Inspector Krogh" in html
    assert "span-gap" in html
    assert 'href="#marker-1"' in html and 'id="marker-1"' in html
    assert "mark-start" in html
    assert 'class="checks-table"' in html


def test_render_html_flags_failed_checks():
    report = _minimal_report(
        checks=[{"event_id": 1, "boundary": "start", "check": "lands_on_idr", "pass": False, "detail": "0.5s off"}],
        summary={"marker_count": 0, "checks_total": 1, "checks_failed": 1},
    )
    html = render_html(report)
    assert "1 check(s) failed" in html


def test_markers_starting_at_the_same_time_are_grouped():
    def mk(eid, code, start):
        ev = {"pts_ticks": round(start * 90000), "pts_seconds": start, "upid_type": None,
              "upid_hex": None, "segment_num": None, "segments_expected": None, "flags": {}}
        return {"event_id": eid, "splice_type": "time_signal", "segmentation_type_id": "0x22",
                "type_name": code, "type_code": code, "is_instant": True, "duration_seconds": None,
                "nesting_depth": 0, "contains": [], "start": ev, "stop": None}

    report = _minimal_report(
        markers=[mk(1, "PAD", 20.0), mk(2, "BRK", 20.0), mk(3, "CAS", 30.0)],
        summary={"marker_count": 3, "checks_total": 0, "checks_failed": 0},
    )
    html = render_html(report)
    assert html.count('<section class="tp-group"') == 2
    assert "2 items" in html and "1 item<" in html
    assert html.index('id="marker-2"') < html.index('id="marker-1"')


def test_asset_joins_without_markers_get_cards_and_timeline_row():
    report = _minimal_report(
        timeline={
            "name": "x.timeline.json", "path": "x.timeline.json",
            "assets": [
                {"asset_id": "a1", "file": "a1.mp4", "start": 0.0, "end": 30.0},
                {"asset_id": "a2", "file": "a2.mp4", "start": 30.0, "end": 60.0},
            ],
            "transitions": [{"index": 1, "time": 30.0, "from_asset": "a1", "from_file": "a1.mp4",
                             "to_asset": "a2", "to_file": "a2.mp4"}],
        },
        frames={"trans1": [{"label": "+0", "pts_seconds": 30.0, "is_idr": True, "file": None}]},
        checks=[{"event_id": None, "transition": 1, "boundary": "join", "check": "lands_on_idr",
                 "pass": True, "detail": "ok"}],
        summary={"marker_count": 0, "checks_total": 1, "checks_failed": 0},
    )
    html = render_html(report)
    assert 'id="transition-1"' in html and "a1 → a2" in html
    assert 'id="tp-30000"' in html and 'href="#tp-30000"' in html
    assert "join 1" in html and "mark-join" in html


def test_missing_sidecars_render_as_warnings_unless_deliberately_disabled():
    html = render_html(_minimal_report())
    assert html.count('<td class="warn">') == 2
    assert "no timeline.json found" in html and "no markers.json found" in html

    quiet = render_html(_minimal_report(sidecars_disabled=True))
    assert '<td class="warn">' not in quiet
    assert "not used (--no-expected)" in quiet
