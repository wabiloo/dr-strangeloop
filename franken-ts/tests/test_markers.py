"""Tests for the nested `markers` feature: config validation, timeline
resolution (containment/segment_num auto-fill), and SCTE-35 emission
(coincident-PTS message merging)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from franken_ts.config import Config
from franken_ts.markers import build_markers
from franken_ts.scte35 import generate_xml
from franken_ts.timeline import TimelineEntry, resolve_markers


def _nested_break_config() -> dict:
    return {
        "output": {"file": "out.ts"},
        "assets": [
            {"file": "content1.mp4", "id": "content1"},
            {"file": "jingle.mp4", "id": "jingle"},
            {"file": "ad1.mp4", "id": "ad1"},
            {"file": "ad2.mp4", "id": "ad2"},
            {"file": "content2.mp4", "id": "content2"},
        ],
        "markers": [
            {"event_id": 100, "splice_type": "time_signal",
             "segmentation": {"type_id": "0x22", "upid_hex": "aa"}, "assets": ["jingle", "ad1", "ad2"]},
            {"event_id": 101, "splice_type": "time_signal",
             "segmentation": {"type_id": "0x34", "upid_hex": "bb"}, "assets": ["jingle"]},
            {"event_id": 102, "splice_type": "time_signal",
             "segmentation": {"type_id": "0x34", "upid_hex": "cc"}, "assets": ["ad1", "ad2"]},
            {"event_id": 103, "splice_type": "time_signal",
             "segmentation": {"type_id": "0x30", "upid_hex": "dd"}, "assets": ["ad1"]},
            {"event_id": 104, "splice_type": "time_signal",
             "segmentation": {"type_id": "0x30", "upid_hex": "ee"}, "assets": ["ad2"]},
        ],
    }


def _entries_from_config(cfg: Config, clip_seconds: float = 10.0) -> list[TimelineEntry]:
    entries = []
    t = 0.0
    for asset in cfg.assets:
        entries.append(TimelineEntry(
            source_file=asset.file, inpoint=0.0, outpoint=clip_seconds,
            output_start=t, output_end=t + clip_seconds,
            inpoint_raw=0.0, outpoint_raw=clip_seconds, asset_id=asset.id,
        ))
        t += clip_seconds
    return entries


# ── Config validation ─────────────────────────────────────────────────────

def test_valid_nested_markers_load():
    cfg = Config.model_validate(_nested_break_config())
    assert len(cfg.markers) == 5
    # `type` is derived FROM segmentation.type_id (never the reverse) --
    # this is the whole point: there is no way for them to disagree.
    assert cfg.markers[0].type == "break"
    assert cfg.markers[0].segmentation.type_id == "0x22"
    assert cfg.markers[1].type == "ppo"
    assert cfg.markers[1].segmentation.type_id == "0x34"
    assert cfg.markers[3].type == "ad"
    assert cfg.markers[3].segmentation.type_id == "0x30"


def test_type_is_computed_not_a_stored_field():
    """`type` isn't part of the schema at all -- it's a read-only property
    computed from segmentation.type_id. A stray `type` key in the YAML
    (e.g. left over from hand-editing) is just ignored, never respected,
    so it can no longer silently disagree with what the marker actually
    signals."""
    raw = _nested_break_config()
    raw["markers"][0]["type"] = "ad"  # stray/wrong; type_id says 0x22 (break) -- ignored
    cfg = Config.model_validate(raw)
    assert cfg.markers[0].type == "break"


def test_marker_referencing_unknown_asset_id_rejected():
    raw = _nested_break_config()
    raw["markers"][0]["assets"] = ["does-not-exist"]
    with pytest.raises(ValidationError, match="unknown asset id"):
        Config.model_validate(raw)


def test_marker_non_contiguous_assets_rejected():
    raw = _nested_break_config()
    # content1 + ad1 skips over jingle -> not contiguous
    raw["markers"][0]["assets"] = ["content1", "ad1"]
    with pytest.raises(ValidationError, match="not a contiguous run"):
        Config.model_validate(raw)


def test_partially_overlapping_markers_rejected():
    raw = _nested_break_config()
    raw["markers"].append({
        "event_id": 200, "splice_type": "time_signal",
        "segmentation": {"upid_hex": "x"}, "assets": ["jingle", "ad1"],
    })
    with pytest.raises(ValidationError, match="partially overlap"):
        Config.model_validate(raw)


def test_identical_spans_rejected():
    raw = _nested_break_config()
    raw["markers"].append({
        "event_id": 200, "splice_type": "time_signal",
        "segmentation": {"upid_hex": "x"}, "assets": ["jingle", "ad1", "ad2"],
    })
    with pytest.raises(ValidationError, match="exact same assets"):
        Config.model_validate(raw)


def test_duplicate_asset_id_rejected():
    raw = _nested_break_config()
    raw["assets"][0]["id"] = "jingle"  # collides with assets[1]
    with pytest.raises(ValidationError, match="Duplicate asset id"):
        Config.model_validate(raw)


def test_duplicate_event_id_across_markers_rejected():
    raw = _nested_break_config()
    raw["markers"].append({
        "event_id": 100,  # collides with the break marker
        "splice_type": "time_signal", "segmentation": {"upid_hex": "x"},
        "assets": ["content1"],
    })
    with pytest.raises(ValidationError, match="Duplicate"):
        Config.model_validate(raw)


# ── Timeline resolution ───────────────────────────────────────────────────

def test_resolve_markers_spans_and_auto_segnum():
    cfg = Config.model_validate(_nested_break_config())
    entries = _entries_from_config(cfg)
    boundaries = resolve_markers(cfg.markers, entries)

    by_event = {}
    for b in boundaries:
        by_event.setdefault(b.event_id, {})[b.is_start] = b

    # Break spans jingle(10s)+ad1(10s)+ad2(10s) starting after content1
    assert by_event[100][True].output_time == 10.0
    assert by_event[100][False].output_time == 40.0
    assert by_event[100][True].marker.segmentation.segment_num == 0
    assert by_event[100][True].marker.segmentation.segments_expected == 1  # only child of root

    # Two PPOs are siblings under the break -> segment_num 0/1 of 2
    assert by_event[101][True].marker.segmentation.segment_num == 0
    assert by_event[101][True].marker.segmentation.segments_expected == 2
    assert by_event[102][True].marker.segmentation.segment_num == 1
    assert by_event[102][True].marker.segmentation.segments_expected == 2

    # Two ads are siblings under the second PPO -> segment_num 0/1 of 2
    assert by_event[103][True].marker.segmentation.segment_num == 0
    assert by_event[103][True].marker.segmentation.segments_expected == 2
    assert by_event[104][True].marker.segmentation.segment_num == 1
    assert by_event[104][True].marker.segmentation.segments_expected == 2


def test_explicit_segment_num_not_overridden():
    raw = _nested_break_config()
    raw["markers"][3]["segmentation"]["segment_num"] = 7
    cfg = Config.model_validate(raw)
    entries = _entries_from_config(cfg)
    boundaries = resolve_markers(cfg.markers, entries)
    ad1_start = next(b for b in boundaries if b.event_id == 103 and b.is_start)
    assert ad1_start.marker.segmentation.segment_num == 7


def test_frame_accuracy_moves_with_asset_durations():
    """Trimming an asset's duration should move every enclosing marker's
    boundary automatically -- nothing to desync since spans are derived."""
    cfg = Config.model_validate(_nested_break_config())
    entries = _entries_from_config(cfg, clip_seconds=10.0)
    # Shrink the jingle (index 1) to 3s and shift everything after it.
    entries[1].output_end = entries[1].output_start + 3.0
    shift = 10.0 - 3.0
    for e in entries[2:]:
        e.output_start -= shift
        e.output_end -= shift

    boundaries = resolve_markers(cfg.markers, entries)
    by_event = {(b.event_id, b.is_start): b for b in boundaries}

    assert by_event[(101, True)].output_time == entries[1].output_start
    assert by_event[(101, False)].output_time == entries[1].output_end
    assert by_event[(100, True)].output_time == entries[1].output_start
    assert by_event[(100, False)].output_time == entries[-2].output_end  # ad2


# ── SCTE-35 emission ──────────────────────────────────────────────────────

def test_coincident_boundaries_merge_into_one_message():
    cfg = Config.model_validate(_nested_break_config())
    entries = _entries_from_config(cfg)
    boundaries = resolve_markers(cfg.markers, entries)
    pts_map = {(b.event_id, b.is_start): round(b.output_time * 90_000) for b in boundaries}

    import xml.etree.ElementTree as ET
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        xml_path = Path(d) / "scte35.xml"
        generate_xml(boundaries, pts_map, xml_path)
        root = ET.fromstring(xml_path.read_text())

    tables = root.findall("splice_information_table")
    # 4 distinct PTS instants: break+ppo1 start, ppo1-end+ppo2-start+ad1-start,
    # ad1-end+ad2-start, ad2-end+ppo2-end+break-end
    assert len(tables) == 4
    descriptor_counts = [len(t.findall("splice_segmentation_descriptor")) for t in tables]
    assert descriptor_counts == [2, 3, 2, 3]


def test_markers_json_includes_type_and_assets():
    cfg = Config.model_validate(_nested_break_config())
    entries = _entries_from_config(cfg)
    boundaries = resolve_markers(cfg.markers, entries)
    pts_map = {(b.event_id, b.is_start): round(b.output_time * 90_000) for b in boundaries}

    out = build_markers(boundaries, pts_map)
    break_start = next(m for m in out if m["event_id"] == "0x00000064" and m["segmentation_type_id"] == "0x22")
    assert break_start["type"] == "break"
    assert break_start["assets"] == ["jingle", "ad1", "ad2"]


# ── Instant (standalone) segmentation types resolve to ONE boundary ───────

def test_instant_segmentation_type_produces_single_boundary():
    """0x13 Program Breakaway has no defined 'end' partner (unlike e.g. 0x22/
    0x23 Break Start/End) -- it should resolve to exactly one AdBoundary, at
    the marker's span start, not a start+stop pair."""
    raw = {
        "output": {"file": "out.ts"},
        "assets": [
            {"file": "content1.mp4", "id": "c1"},
            {"file": "ad1.mp4", "id": "ad1"},
        ],
        "markers": [
            {"event_id": 1, "splice_type": "time_signal",
             "segmentation": {"type_id": "0x13", "upid_hex": "aa"}, "assets": ["ad1"]},
        ],
    }
    cfg = Config.model_validate(raw)
    entries = _entries_from_config(cfg)
    boundaries = resolve_markers(cfg.markers, entries)

    assert len(boundaries) == 1
    assert boundaries[0].is_start is True
    assert boundaries[0].output_time == entries[1].output_start


def test_paired_segmentation_type_still_produces_two_boundaries():
    raw = {
        "output": {"file": "out.ts"},
        "assets": [
            {"file": "content1.mp4", "id": "c1"},
            {"file": "ad1.mp4", "id": "ad1"},
        ],
        "markers": [
            {"event_id": 1, "splice_type": "time_signal",
             "segmentation": {"type_id": "0x30", "upid_hex": "aa"}, "assets": ["ad1"]},
        ],
    }
    cfg = Config.model_validate(raw)
    entries = _entries_from_config(cfg)
    boundaries = resolve_markers(cfg.markers, entries)
    assert len(boundaries) == 2
    assert {b.is_start for b in boundaries} == {True, False}


def test_instant_marker_scte35_emission_single_message():
    raw = {
        "output": {"file": "out.ts"},
        "assets": [
            {"file": "content1.mp4", "id": "c1"},
            {"file": "ad1.mp4", "id": "ad1"},
        ],
        "markers": [
            {"event_id": 1, "splice_type": "time_signal",
             "segmentation": {"type_id": "0x13", "upid_hex": "aa"}, "assets": ["ad1"]},
        ],
    }
    cfg = Config.model_validate(raw)
    entries = _entries_from_config(cfg)
    boundaries = resolve_markers(cfg.markers, entries)
    pts_map = {(b.event_id, b.is_start): round(b.output_time * 90_000) for b in boundaries}

    import tempfile
    import xml.etree.ElementTree as ET
    with tempfile.TemporaryDirectory() as d:
        xml_path = Path(d) / "scte35.xml"
        generate_xml(boundaries, pts_map, xml_path)
        root = ET.fromstring(xml_path.read_text())

    tables = root.findall("splice_information_table")
    assert len(tables) == 1
    descs = tables[0].findall("splice_segmentation_descriptor")
    assert len(descs) == 1
    assert descs[0].get("segmentation_type_id") == "0x13"


# ── Legacy single-asset `ad_break` is no longer accepted ──────────────────

def test_legacy_asset_level_ad_break_field_has_no_effect():
    """The old per-asset `ad_break` block was replaced entirely by the flat
    `markers` list -- AssetConfig no longer declares the field, so it's
    silently ignored (pydantic's default extra-field handling) rather than
    erroring; this documents that it creates no marker."""
    raw = {
        "output": {"file": "out.ts"},
        "assets": [
            {"file": "content.mp4"},
            {"file": "ad.mp4", "id": "ad1",
             "ad_break": {"event_id": 1, "splice_type": "splice_insert"}},
        ],
    }
    cfg = Config.model_validate(raw)
    assert cfg.markers == []
    assert not hasattr(cfg.assets[1], "ad_break")
