"""Expected-vs-actual comparison: pure dict logic, no tsduck/ffmpeg."""

from __future__ import annotations

from krogh_expected import compare_expected, discover_expected_path

FPS = 25.0


def _actual(eid, boundary, ticks, **kw):
    ev = {"pts_ticks": ticks, "pts_seconds": ticks / 90000, "type_id": "0x22" if boundary == "start" else "0x23",
          "segmentation_duration_ticks": 2_700_000 if boundary == "start" else None,
          "upid_type": "0x09", "upid_hex": "42 52 45 41 4B 31", "segment_num": 1, "segments_expected": 1,
          "flags": {"web_delivery_allowed": True, "no_regional_blackout": False,
                    "archive_allowed": False, "device_restrictions": 1}}
    ev.update(kw)
    m = {"event_id": eid, "start": None, "stop": None}
    m[boundary] = ev
    return m


def _expected(eid, is_out, ticks, **kw):
    e = {"event_id": f"0x{eid:08X}", "is_out": is_out, "pts_time_ticks": ticks,
         "segmentation_type_id": "0x22" if is_out else "0x23",
         "segmentation_duration_ticks": 2_700_000 if is_out else 0,
         "upid_type": "0x09", "upid_hex": "42 52 45 41 4b 31", "segment_num": 1, "segments_expected": 1,
         "assets": ["a1", "a2"],
         "flags": {"web_delivery_allowed": True, "no_regional_blackout": False,
                   "archive_allowed": False, "device_restrictions": 1}}
    e.update(kw)
    return e


def test_matching_entry_passes_and_attaches_assets():
    m = _actual(100, "start", 1_800_000)
    checks = compare_expected([_expected(100, True, 1_800_000)], [m], FPS)
    assert len(checks) == 1 and checks[0]["check"] == "matches_expected" and checks[0]["pass"]
    assert m["assets"] == ["a1", "a2"]


def test_pts_beyond_one_frame_and_field_mismatches_fail():
    m = _actual(100, "start", 1_800_000 + 9000, upid_hex="00", segment_num=2,
                flags={"web_delivery_allowed": False, "no_regional_blackout": False,
                       "archive_allowed": False, "device_restrictions": 2})
    (c,) = compare_expected([_expected(100, True, 1_800_000)], [m], FPS)
    assert not c["pass"]
    for needle in ("pts +0.100s", "upid expected", "segment_num", "web_delivery_allowed", "device_restrictions"):
        assert needle in c["detail"]


def test_pts_within_one_frame_is_fine():
    m = _actual(100, "start", 1_800_000 + 3000)
    (c,) = compare_expected([_expected(100, True, 1_800_000)], [m], FPS)
    assert c["pass"]


def test_missing_and_unexpected_markers_are_failures():
    checks = compare_expected(
        [_expected(100, True, 1_800_000)],
        [_actual(200, "start", 900_000)],
        FPS,
    )
    by = {c["check"]: c for c in checks}
    assert not by["expected_present"]["pass"] and by["expected_present"]["event_id"] == 100
    assert not by["unexpected_marker"]["pass"] and by["unexpected_marker"]["event_id"] == 200


def test_stop_boundary_ignores_declared_duration():
    m = _actual(100, "stop", 4_500_000)
    (c,) = compare_expected([_expected(100, False, 4_500_000)], [m], FPS)
    assert c["boundary"] == "stop" and c["pass"]


def test_discover_expected_path(tmp_path):
    ts = tmp_path / "loop.ts"
    ts.write_bytes(b"")
    assert discover_expected_path(ts) is None
    (tmp_path / "markers.json").write_text("[]")
    assert discover_expected_path(ts) == tmp_path / "markers.json"
    (tmp_path / "loop.markers.json").write_text("[]")
    assert discover_expected_path(ts) == tmp_path / "loop.markers.json"


def test_find_transitions_skips_joins_that_coincide_with_a_marker():
    from krogh_expected import find_transitions, timeline_assets

    doc = {"muxer_offset": 0.0, "entries": [
        {"asset_id": "a", "source_file": "a.mp4", "output_start": 0.0, "output_end": 20.0},
        {"asset_id": "b", "source_file": "b.mp4", "output_start": 20.0, "output_end": 30.0},
        {"asset_id": "c", "source_file": "c.mp4", "output_start": 30.0, "output_end": 40.0},
    ]}
    markers = [{"start": {"pts_seconds": 20.0}, "stop": {"pts_seconds": 40.0}}]
    (tr,) = find_transitions(timeline_assets(doc), markers, FPS)
    assert tr["time"] == 30.0 and tr["from_asset"] == "b" and tr["to_asset"] == "c" and tr["index"] == 1


def test_timeline_assets_apply_muxer_offset():
    from krogh_expected import timeline_assets

    (a,) = timeline_assets({"muxer_offset": 1.5, "entries": [
        {"asset_id": "a", "source_file": "a.mp4", "output_start": 0.0, "output_end": 2.0}]})
    assert a["start"] == 1.5 and a["end"] == 3.5
