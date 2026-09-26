"""Tests for krogh's pure logic: XML parsing, start/stop pairing, and
nesting inference -- all exercised against a synthetic tsduck `--xml` dump
so they run without `tsp`/`ffmpeg` installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import krogh as scan

PTS_CLOCK = 90_000


def _write_xml(tmp_path: Path, body: str) -> Path:
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<tsduck>\n{body}\n</tsduck>\n'
    path = tmp_path / "splice.xml"
    path.write_text(xml)
    return path


def _splice_insert_table(event_id: int, pts_seconds: float, out_of_network: bool) -> str:
    pts = round(pts_seconds * PTS_CLOCK)
    return f"""
  <splice_information_table>
    <splice_insert splice_event_id="0x{event_id:X}" out_of_network="{"true" if out_of_network else "false"}"
                    pts_time="{pts}"/>
  </splice_information_table>"""


def _time_signal_table(pts_seconds: float, descriptors: list[dict]) -> str:
    pts = round(pts_seconds * PTS_CLOCK)
    descs = "\n".join(
        f'    <splice_segmentation_descriptor segmentation_event_id="0x{d["event_id"]:X}" '
        f'segmentation_type_id="0x{d["type_id"]:02X}" '
        + (f'segmentation_duration="{round(d["duration"] * PTS_CLOCK)}" ' if d.get("duration") else "")
        + "/>"
        for d in descriptors
    )
    return f"""
  <splice_information_table>
    <time_signal pts_time="{pts}"/>
{descs}
  </splice_information_table>"""


# ── parsing ──────────────────────────────────────────────────────────────────

def test_parse_splice_insert_pair(tmp_path):
    xml = _write_xml(tmp_path, (
        _splice_insert_table(1, 10.0, out_of_network=True)
        + _splice_insert_table(1, 40.0, out_of_network=False)
    ))
    events = scan.parse_scte35_xml(xml)
    assert len(events) == 2
    assert events[0].is_start is True
    assert events[0].pts_seconds == pytest.approx(10.0)
    assert events[1].is_start is False
    assert events[1].pts_seconds == pytest.approx(40.0)


def test_parse_time_signal_multiple_concurrent_descriptors(tmp_path):
    """A single time_signal table carrying a Break-start AND a
    Provider-Ad-start descriptor at the same PTS -- exactly how concurrent
    segmentation types are signalled on the wire."""
    xml = _write_xml(tmp_path, _time_signal_table(20.0, [
        {"event_id": 1, "type_id": 0x22},  # Break Start
        {"event_id": 2, "type_id": 0x30},  # Provider Advertisement Start
    ]))
    events = scan.parse_scte35_xml(xml)
    assert len(events) == 2
    assert {e.event_id for e in events} == {1, 2}
    assert all(e.pts_seconds == pytest.approx(20.0) for e in events)
    assert {e.segmentation_type_id for e in events} == {0x22, 0x30}


def test_parse_empty_file_returns_no_events(tmp_path):
    xml = _write_xml(tmp_path, "")
    assert scan.parse_scte35_xml(xml) == []


def test_parse_missing_file_returns_no_events(tmp_path):
    assert scan.parse_scte35_xml(tmp_path / "does_not_exist.xml") == []


# ── pairing ──────────────────────────────────────────────────────────────────

def test_pair_splice_insert_start_stop():
    events = [
        scan.RawEvent(1, "splice_insert", True, False, 900_000, 10.0),
        scan.RawEvent(1, "splice_insert", False, False, 3_600_000, 40.0),
    ]
    markers = scan.pair_events_into_markers(events)
    assert len(markers) == 1
    m = markers[0]
    assert m.start is not None and m.stop is not None
    assert m.duration_seconds == pytest.approx(30.0)


def test_pair_time_signal_break_and_end():
    events = [
        scan.RawEvent(5, "time_signal", True, False, 900_000, 10.0, segmentation_type_id=0x22),
        scan.RawEvent(5, "time_signal", False, False, 3_600_000, 40.0, segmentation_type_id=0x23),
    ]
    markers = scan.pair_events_into_markers(events)
    assert len(markers) == 1
    assert markers[0].type_label == "Break"
    assert markers[0].duration_seconds == pytest.approx(30.0)


def test_instant_signal_has_no_stop():
    events = [scan.RawEvent(9, "time_signal", True, True, 900_000, 10.0, segmentation_type_id=0x01)]
    markers = scan.pair_events_into_markers(events)
    assert len(markers) == 1
    assert markers[0].is_instant is True
    assert markers[0].stop is None


def test_orphan_stop_with_no_start_is_still_reported():
    events = [scan.RawEvent(3, "time_signal", False, False, 900_000, 10.0, segmentation_type_id=0x23)]
    markers = scan.pair_events_into_markers(events)
    assert len(markers) == 1
    assert markers[0].start is None
    assert markers[0].stop is not None


# ── nesting ──────────────────────────────────────────────────────────────────

def test_infer_nesting_depth_and_containment():
    # Break [0, 30) contains PPO [5, 25) contains Ad [10, 20).
    brk = scan.Marker(1, "time_signal", 0x22, False,
                       start=scan.RawEvent(1, "time_signal", True, False, 0, 0.0, 0x22),
                       stop=scan.RawEvent(1, "time_signal", False, False, 0, 30.0, 0x23))
    ppo = scan.Marker(2, "time_signal", 0x34, False,
                       start=scan.RawEvent(2, "time_signal", True, False, 0, 5.0, 0x34),
                       stop=scan.RawEvent(2, "time_signal", False, False, 0, 25.0, 0x35))
    ad = scan.Marker(3, "time_signal", 0x30, False,
                      start=scan.RawEvent(3, "time_signal", True, False, 0, 10.0, 0x30),
                      stop=scan.RawEvent(3, "time_signal", False, False, 0, 20.0, 0x31))

    markers = [brk, ppo, ad]
    scan.infer_nesting(markers)

    assert brk.nesting_depth == 0
    assert ppo.nesting_depth == 1
    assert ad.nesting_depth == 2
    assert brk.contains == [2]     # immediate child only, not the grandchild
    assert ppo.contains == [3]
    assert ad.contains == []


def test_infer_nesting_sibling_spans_are_not_nested():
    a = scan.Marker(1, "time_signal", 0x30, False,
                     start=scan.RawEvent(1, "time_signal", True, False, 0, 0.0, 0x30),
                     stop=scan.RawEvent(1, "time_signal", False, False, 0, 10.0, 0x31))
    b = scan.Marker(2, "time_signal", 0x30, False,
                     start=scan.RawEvent(2, "time_signal", True, False, 0, 10.0, 0x30),
                     stop=scan.RawEvent(2, "time_signal", False, False, 0, 20.0, 0x31))
    scan.infer_nesting([a, b])
    assert a.nesting_depth == 0
    assert b.nesting_depth == 0
    assert a.contains == []
    assert b.contains == []


# ── checks ───────────────────────────────────────────────────────────────────

def test_lands_on_idr_check_passes_within_tolerance():
    m = scan.Marker(1, "splice_insert", None, False,
                     start=scan.RawEvent(1, "splice_insert", True, False, 900_000, 10.0))
    checks = scan.build_checks([m], idr_times=[10.01], frame_dur=1 / 25,
                                idr_tolerance_frames=1.0, duration_tolerance_frames=2.0)
    idr_checks = [c for c in checks if c["check"] == "lands_on_idr"]
    assert len(idr_checks) == 1
    assert idr_checks[0]["pass"] is True


def test_lands_on_idr_check_fails_outside_tolerance():
    m = scan.Marker(1, "splice_insert", None, False,
                     start=scan.RawEvent(1, "splice_insert", True, False, 900_000, 10.0))
    checks = scan.build_checks([m], idr_times=[11.0], frame_dur=1 / 25,
                                idr_tolerance_frames=1.0, duration_tolerance_frames=2.0)
    idr_checks = [c for c in checks if c["check"] == "lands_on_idr"]
    assert idr_checks[0]["pass"] is False


def test_missing_stop_flagged_as_failing_check():
    m = scan.Marker(1, "time_signal", 0x22, False,
                     start=scan.RawEvent(1, "time_signal", True, False, 0, 10.0, 0x22))
    checks = scan.build_checks([m], idr_times=[10.0], frame_dur=1 / 25,
                                idr_tolerance_frames=1.0, duration_tolerance_frames=2.0)
    stop_checks = [c for c in checks if c["check"] == "has_matching_stop"]
    assert len(stop_checks) == 1
    assert stop_checks[0]["pass"] is False
