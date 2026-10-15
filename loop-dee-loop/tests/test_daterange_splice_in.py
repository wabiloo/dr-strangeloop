"""A bare splice_insert splice-in updates the range its splice-out opened: the
OUT's ID, START-DATE and PLANNED-DURATION, SCTE35-IN instead of SCTE35-OUT, and
an END-DATE where the break closes."""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scte35_signaling import (  # noqa: E402
    build_daterange_tags,
    build_grouped_daterange_tags,
    markers_to_signaling,
)

TS = 90_000
START = dt.datetime(2026, 1, 1, 0, 0, 0)


def _marker(pts_s: float, is_out: bool, duration_s: float | None = None, **extra) -> dict:
    m = {
        "event_id": "0x0000000C",
        "splice_type": "splice_insert",
        "pts_time_ticks": round(pts_s * TS),
        "is_out": is_out,
        "splice_command_b64": "AAEC" if is_out else "AwQF",
    }
    if duration_s is not None:
        m["segmentation_duration_ticks"] = round(duration_s * TS)
    m.update(extra)
    return m


def _attrs(tag: str) -> dict[str, str]:
    return {k: v.strip('"') for k, v in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', tag.split(":", 1)[1])}


MARKERS = [_marker(10, True, 15), _marker(25, False, 0)]


def test_splice_in_updates_the_out_range():
    out_tag, in_tag = (
        build_daterange_tags(markers_to_signaling([m], all_markers=MARKERS), TS, START)[0] for m in MARKERS
    )
    out, back = _attrs(out_tag), _attrs(in_tag)
    assert out["ID"] == back["ID"] == "SPIs-12-0"
    assert out["START-DATE"] == back["START-DATE"] == "2026-01-01T00:00:10.000Z"
    assert out["PLANNED-DURATION"] == back["PLANNED-DURATION"] == "15.000"
    assert back["END-DATE"] == "2026-01-01T00:00:25.000Z"
    assert "END-DATE" not in out
    assert "SCTE35-OUT" in out and "SCTE35-IN" not in out
    assert "SCTE35-IN" in back and "SCTE35-OUT" not in back


def test_splice_in_alone_in_the_window_still_references_its_out():
    # The OUT scrolled out of the window: only the IN is rendered.
    (tag,) = build_daterange_tags(markers_to_signaling([MARKERS[1]], all_markers=MARKERS), TS, START)
    a = _attrs(tag)
    assert (a["ID"], a["START-DATE"], a["END-DATE"]) == (
        "SPIs-12-0", "2026-01-01T00:00:10.000Z", "2026-01-01T00:00:25.000Z",
    )


def test_splice_in_without_a_known_out_keeps_its_own_range():
    (tag,) = build_daterange_tags(markers_to_signaling([MARKERS[1]]), TS, START)
    a = _attrs(tag)
    assert a["ID"] == "SPIe-12-0" and a["START-DATE"] == "2026-01-01T00:00:25.000Z"
    assert "END-DATE" not in a


def test_each_loop_pairs_with_its_own_out_and_identity():
    markers = [
        _marker(10, True, 5, marker_identity=0), _marker(15, False, 0, marker_identity=0),
        _marker(30, True, 8, marker_identity=1), _marker(38, False, 0, marker_identity=1),
    ]
    tags = build_daterange_tags(markers_to_signaling(markers[3:], all_markers=markers), TS, START)
    a = _attrs(tags[0])
    assert a["START-DATE"] == "2026-01-01T00:00:30.000Z" and a["PLANNED-DURATION"] == "8.000"
    assert a["END-DATE"] == "2026-01-01T00:00:38.000Z" and a["ID"].endswith("-1")


def test_legacy_id_format_also_shares_the_outs_id():
    out_tag, in_tag = (
        build_daterange_tags(markers_to_signaling([m], all_markers=MARKERS), TS, START, daterange_id_format=None)[0]
        for m in MARKERS
    )
    assert _attrs(out_tag)["ID"] == _attrs(in_tag)["ID"]


def test_grouped_mode_single_marker_group_follows_the_same_rule():
    (tag,) = build_grouped_daterange_tags(markers_to_signaling([MARKERS[1]], all_markers=MARKERS), TS, START)
    a = _attrs(tag)
    assert a["ID"] == "SPIs-12-0" and a["END-DATE"] == "2026-01-01T00:00:25.000Z"


def test_time_signal_pairs_are_untouched():
    start = {"event_id": "0x1", "splice_type": "time_signal", "pts_time_ticks": 10 * TS, "is_out": True,
             "segmentation_type_id": "0x22", "segmentation_duration_ticks": 15 * TS, "splice_command_b64": "AAEC"}
    stop = {"event_id": "0x1", "splice_type": "time_signal", "pts_time_ticks": 25 * TS, "is_out": False,
            "segmentation_type_id": "0x23", "segmentation_duration_ticks": 0, "splice_command_b64": "AwQF"}
    (tag,) = build_daterange_tags(markers_to_signaling([stop], all_markers=[start, stop]), TS, START)
    a = _attrs(tag)
    assert a["START-DATE"] == "2026-01-01T00:00:25.000Z" and "END-DATE" not in a
