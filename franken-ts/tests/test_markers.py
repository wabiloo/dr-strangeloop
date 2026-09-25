"""Tests for the nested `markers` feature: config validation, timeline
resolution (containment/segment_num auto-fill), and SCTE-35 emission
(coincident-PTS message merging)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from franken_ts.config import Config, load_config
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
    """Same splice_type + same segmentation.type_id over the exact same
    assets as an existing marker (0x22, event 100) -- a true duplicate."""
    raw = _nested_break_config()
    raw["markers"].append({
        "event_id": 200, "splice_type": "time_signal",
        "segmentation": {"type_id": "0x22", "upid_hex": "x"}, "assets": ["jingle", "ad1", "ad2"],
    })
    with pytest.raises(ValidationError, match="exact same assets"):
        Config.model_validate(raw)


def test_identical_spans_with_different_signal_allowed():
    """Two markers can validly cover the exact same assets as long as they
    signal different things (different splice_type or segmentation.type_id)
    -- e.g. a Provider Placement Opportunity span (0x34) and a Call Ad
    Server instant (0x02) over the same assets are two distinct SCTE-35
    messages, not a duplicate."""
    raw = _nested_break_config()
    raw["markers"].append({
        "event_id": 200, "splice_type": "time_signal",
        "segmentation": {"type_id": "0x02", "upid_hex": "x"}, "assets": ["jingle", "ad1", "ad2"],
    })
    Config.model_validate(raw)  # should not raise -- 0x22 vs 0x02, different signal


def test_identical_instant_spans_with_same_type_id_rejected():
    """Two instant markers signaling the same exact thing (same type_id)
    over the same assets ARE a duplicate -- same exact instant, same
    signal, twice."""
    raw = _nested_break_config()
    raw["markers"].append({
        "event_id": 200, "splice_type": "time_signal",
        "segmentation": {"type_id": "0x02", "upid_hex": "x"}, "assets": ["ad1"],
    })
    raw["markers"].append({
        "event_id": 201, "splice_type": "time_signal",
        "segmentation": {"type_id": "0x02", "upid_hex": "y"}, "assets": ["ad1"],
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
    assert by_event[100][True].marker.segmentation.segments_expected == 0  # no enclosing Program collection

    # One Break-local PO set inherits the Break numbering (unnumbered here).
    assert by_event[101][True].marker.segmentation.segment_num == 0
    assert by_event[101][True].marker.segmentation.segments_expected == 0
    assert by_event[102][True].marker.segmentation.segment_num == 0
    assert by_event[102][True].marker.segmentation.segments_expected == 0

    assert [(by_event[e][True].marker.segmentation.sub_segment_num,
             by_event[e][True].marker.segmentation.sub_segments_expected) for e in (101, 102)] == [(1, 2), (2, 2)]
    # The ad counter is independent of the PO counter.
    assert by_event[103][True].marker.segmentation.segment_num == 0
    assert by_event[103][True].marker.segmentation.segments_expected == 0
    assert by_event[104][True].marker.segmentation.segment_num == 0
    assert by_event[104][True].marker.segmentation.segments_expected == 0
    assert [(by_event[e][True].marker.segmentation.sub_segment_num,
             by_event[e][True].marker.segmentation.sub_segments_expected) for e in (103, 104)] == [(1, 2), (2, 2)]


def test_explicit_segment_num_not_overridden():
    raw = _nested_break_config()
    raw["enforce_scte35_marker_semantics"] = False
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


# ── Table 23 segmentation pairing and standalone signals ─────────────────

def test_instant_segmentation_type_produces_single_boundary():
    """Program Early Termination remains a one-boundary instant signal."""
    raw = {
        "output": {"file": "out.ts"},
        "assets": [
            {"file": "content1.mp4", "id": "c1"},
            {"file": "ad1.mp4", "id": "ad1"},
        ],
        "markers": [
            {"event_id": 1, "splice_type": "time_signal",
             "segmentation": {"type_id": "0x12", "upid_hex": "aa"}, "assets": ["ad1"]},
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
    assert [b.segmentation_type_id for b in boundaries] == [0x30, 0x31]


def test_program_early_termination_can_still_be_used_as_standalone_instant():
    raw = {
        "output": {"file": "out.ts"},
        "assets": [{"file": "content.mp4", "id": "content"}],
        "markers": [{
            "event_id": 1,
            "splice_type": "time_signal",
            "segmentation": {"type_id": "0x12", "upid_hex": "aa"},
            "assets": ["content"],
        }],
    }
    cfg = Config.model_validate(raw)
    boundaries = resolve_markers(cfg.markers, _entries_from_config(cfg))
    markers = build_markers(boundaries, {(1, True): 0})

    assert len(boundaries) == 1
    assert markers[0]["segmentation_type_id"] == "0x12"
    assert markers[0]["is_instant"] is True


def test_program_end_pair_boundary_is_not_standalone_instant():
    raw = {
        "output": {"file": "out.ts"},
        "assets": [{"file": "program.mp4", "id": "program"}],
        "markers": [{
            "event_id": 1,
            "splice_type": "time_signal",
            "segmentation": {"type_id": "0x17", "upid_hex": "aa"},
            "assets": ["program"],
        }],
    }
    cfg = Config.model_validate(raw)
    boundaries = resolve_markers(cfg.markers, _entries_from_config(cfg))
    markers = build_markers(boundaries, {(1, True): 0, (1, False): 900_000})

    assert [marker["segmentation_type_id"] for marker in markers] == ["0x17", "0x11"]
    assert [marker["is_instant"] for marker in markers] == [False, False]
    assert markers[1]["segmentation_duration_ticks"] == 0
    assert [marker["is_out"] for marker in markers] == [True, False]


@pytest.mark.parametrize(
    ("start_type_id", "end_type_id"),
    [("0x10", 0x11), ("0x13", 0x14), ("0x17", 0x11), ("0x19", 0x11)],
)
def test_program_segmentation_uses_table_23_pairings(start_type_id, end_type_id):
    raw = {
        "output": {"file": "out.ts"},
        "assets": [{"file": "content.mp4", "id": "content"}, {"file": "next.mp4", "id": "next"}],
        "markers": [{
            "event_id": 1,
            "splice_type": "time_signal",
            "segmentation": {"type_id": start_type_id, "upid_hex": "aa"},
            "assets": ["content", "next"],
        }],
    }
    cfg = Config.model_validate(raw)
    entries = _entries_from_config(cfg)
    boundaries = resolve_markers(cfg.markers, entries)

    assert [boundary.segmentation_type_id for boundary in boundaries] == [int(start_type_id, 16), end_type_id]
    assert [boundary.is_start for boundary in boundaries] == [True, False]


def test_program_overlap_xml_and_sidecar_emit_program_end():
    raw = {
        "output": {"file": "out.ts"},
        "assets": [{"file": "program.mp4", "id": "program"}],
        "markers": [{
            "event_id": 1,
            "splice_type": "time_signal",
            "segmentation": {"type_id": "0x17", "upid_hex": "aa"},
            "assets": ["program"],
        }],
    }
    cfg = Config.model_validate(raw)
    boundaries = resolve_markers(cfg.markers, _entries_from_config(cfg))
    pts_map = {(b.event_id, b.is_start): round(b.output_time * 90_000) for b in boundaries}
    markers = build_markers(boundaries, pts_map)

    import tempfile
    import xml.etree.ElementTree as ET
    with tempfile.TemporaryDirectory() as directory:
        xml_path = Path(directory) / "scte35.xml"
        generate_xml(boundaries, pts_map, xml_path)
        root = ET.fromstring(xml_path.read_text())

    emitted_ids = [
        descriptor.get("segmentation_type_id")
        for descriptor in root.iter("splice_segmentation_descriptor")
    ]
    assert emitted_ids == ["0x17", "0x11"]
    assert [marker["segmentation_type_id"] for marker in markers] == emitted_ids


def test_config_rejects_end_type_as_start_marker():
    raw = {
        "output": {"file": "out.ts"},
        "assets": [{"file": "ad.mp4", "id": "ad"}],
        "markers": [{
            "event_id": 1,
            "splice_type": "time_signal",
            "segmentation": {"type_id": "0x21", "upid_hex": "aa"},
            "assets": ["ad"],
        }],
    }
    with pytest.raises(ValidationError, match="segmentation End type"):
        Config.model_validate(raw)


def test_enforcement_toggle_defaults_on_and_is_persisted_by_model():
    cfg = Config.model_validate(_nested_break_config())
    assert cfg.enforce_scte35_marker_semantics is True
    relaxed = Config.model_validate({**_nested_break_config(), "enforce_scte35_marker_semantics": False})
    assert relaxed.enforce_scte35_marker_semantics is False


def test_relaxed_mode_allows_duplicate_ids_and_crossing_semantic_spans():
    raw = {
        "output": {"file": "out.ts"},
        "enforce_scte35_marker_semantics": False,
        "assets": [{"file": f"a{i}.mp4", "id": f"a{i}"} for i in range(5)],
        "markers": [
            {"event_id": 1, "splice_type": "time_signal", "segmentation": {"type_id": "0x22"}, "assets": ["a1", "a2", "a3"]},
            {"event_id": 1, "splice_type": "time_signal", "segmentation": {"type_id": "0x34"}, "assets": ["a3", "a4"]},
        ],
    }
    cfg = Config.model_validate(raw)
    boundaries = resolve_markers(cfg.markers, _entries_from_config(cfg))
    assert {b.marker_index for b in boundaries} == {0, 1}
    pts_map = {("marker", b.marker_index, b.is_start): round(b.output_time * 90_000) for b in boundaries}
    output = build_markers(boundaries, pts_map)
    assert {m["marker_identity"] for m in output} == {0, 1}


def test_strict_rejects_placement_opportunity_crossing_break_boundary():
    raw = {
        "output": {"file": "out.ts"},
        "assets": [{"file": f"a{i}.mp4", "id": f"a{i}"} for i in range(5)],
        "markers": [
            {"event_id": 1, "splice_type": "time_signal", "segmentation": {"type_id": "0x22"}, "assets": ["a1", "a2", "a3"]},
            {"event_id": 2, "splice_type": "time_signal", "segmentation": {"type_id": "0x34"}, "assets": ["a3", "a4"]},
        ],
    }
    with pytest.raises(ValidationError, match="hierarchy"):
        Config.model_validate(raw)


def test_strict_allows_pplacement_outside_break_and_nested_placement_opportunities():
    raw = {
        "output": {"file": "out.ts"},
        "assets": [{"file": f"a{i}.mp4", "id": f"a{i}"} for i in range(6)],
        "markers": [
            {"event_id": 1, "splice_type": "time_signal", "segmentation": {"type_id": "0x22"}, "assets": ["a1", "a2", "a3", "a4"]},
            {"event_id": 2, "splice_type": "time_signal", "segmentation": {"type_id": "0x34"}, "assets": ["a1", "a2", "a3"]},
            {"event_id": 3, "splice_type": "time_signal", "segmentation": {"type_id": "0x36"}, "assets": ["a2"]},
            {"event_id": 4, "splice_type": "time_signal", "segmentation": {"type_id": "0x34"}, "assets": ["a5"]},
        ],
    }
    Config.model_validate(raw)


def test_strict_rejects_ad_promo_same_level_overlap_and_same_kind_nesting():
    raw = {
        "output": {"file": "out.ts"},
        "assets": [{"file": f"a{i}.mp4", "id": f"a{i}"} for i in range(4)],
        "markers": [
            {"event_id": 1, "splice_type": "time_signal", "segmentation": {"type_id": "0x30"}, "assets": ["a1", "a2"]},
            {"event_id": 2, "splice_type": "time_signal", "segmentation": {"type_id": "0x3c"}, "assets": ["a2", "a3"]},
        ],
    }
    with pytest.raises(ValidationError, match="same advertising segmentation level"):
        Config.model_validate(raw)


def test_chapters_may_overlap_and_are_numbered_within_program():
    raw = {
        "output": {"file": "out.ts"},
        "assets": [{"file": f"a{i}.mp4", "id": f"a{i}"} for i in range(5)],
        "markers": [
            {"event_id": 1, "splice_type": "time_signal", "segmentation": {"type_id": "0x10"}, "assets": [f"a{i}" for i in range(5)]},
            {"event_id": 2, "splice_type": "time_signal", "segmentation": {"type_id": "0x20"}, "assets": ["a1", "a2", "a3"]},
            {"event_id": 3, "splice_type": "time_signal", "segmentation": {"type_id": "0x20"}, "assets": ["a2", "a3", "a4"]},
        ],
    }
    cfg = Config.model_validate(raw)
    chapters = [marker for marker in cfg.markers if marker.segmentation.type_id == "0x20"]
    assert [(m.segmentation.segment_num, m.segmentation.segments_expected) for m in chapters] == [(1, 2), (2, 2)]


def test_break_numbering_is_one_based_and_propagates_to_placement_opportunity():
    raw = {
        "output": {"file": "out.ts"},
        "break_numbering_supported": True,
        "assets": [{"file": f"a{i}.mp4", "id": f"a{i}"} for i in range(6)],
        "markers": [
            {"event_id": 1, "splice_type": "time_signal", "segmentation": {"type_id": "0x10"}, "assets": [f"a{i}" for i in range(6)]},
            {"event_id": 2, "splice_type": "time_signal", "segmentation": {"type_id": "0x22"}, "assets": ["a1", "a2"]},
            {"event_id": 3, "splice_type": "time_signal", "segmentation": {"type_id": "0x22"}, "assets": ["a3", "a4"]},
            {"event_id": 4, "splice_type": "time_signal", "segmentation": {"type_id": "0x34"}, "assets": ["a1"]},
        ],
    }
    cfg = Config.model_validate(raw)
    values = {m.event_id: (m.segmentation.segment_num, m.segmentation.segments_expected) for m in cfg.markers}
    assert values[2] == (1, 2)
    assert values[3] == (2, 2)
    assert values[4] == (1, 2)


def test_2023_numbering_independent_tracks_and_first_ad_in_block(tmp_path):
    raw = {
        "output": {"file": "out.ts"}, "break_numbering_supported": True,
        "assets": [{"file": f"a{i}.mp4", "id": f"a{i}"} for i in range(8)],
        "markers": [
            {"event_id": 1, "splice_type": "time_signal", "segmentation": {"type_id": "0x10"}, "assets": [f"a{i}" for i in range(8)]},
            *({"event_id": 10 + i, "splice_type": "time_signal", "segmentation": {"type_id": "0x22"}, "assets": [f"a{j}" for j in indices]}
              for i, indices in enumerate(([1], [2, 3, 4, 5], [6]))),
            {"event_id": 20, "splice_type": "time_signal", "segmentation": {"type_id": "0x34"}, "assets": ["a2", "a3", "a4", "a5"]},
            {"event_id": 21, "splice_type": "time_signal", "segmentation": {"type_id": "0x36"}, "assets": ["a3"]},
            {"event_id": 22, "splice_type": "time_signal", "segmentation": {"type_id": "0x36"}, "assets": ["a4", "a5"]},
            {"event_id": 30, "splice_type": "time_signal", "segmentation": {"type_id": "0x30"}, "assets": ["a2"]},
            {"event_id": 31, "splice_type": "time_signal", "segmentation": {"type_id": "0x3c"}, "assets": ["a3"]},
            {"event_id": 32, "splice_type": "time_signal", "segmentation": {"type_id": "0x44"}, "assets": ["a4", "a5"]},
            {"event_id": 33, "splice_type": "time_signal", "segmentation": {"type_id": "0x32"}, "assets": ["a4"]},
            {"event_id": 34, "splice_type": "time_signal", "segmentation": {"type_id": "0x3e"}, "assets": ["a5"]},
        ],
    }
    cfg = Config.model_validate(raw)
    numbers = {m.event_id: (m.segmentation.segment_num, m.segmentation.segments_expected,
                            m.segmentation.sub_segment_num, m.segmentation.sub_segments_expected) for m in cfg.markers}
    assert numbers[11] == (2, 3, None, None)
    assert [numbers[i] for i in (20, 21, 22)] == [(2, 3, 1, 3), (2, 3, 2, 3), (2, 3, 3, 3)]
    assert [numbers[i] for i in (30, 31, 33, 34)] == [(2, 3, i, 4) for i in range(1, 5)]
    assert numbers[32] == (2, 3, 3, 1)

    boundaries = resolve_markers(cfg.markers, _entries_from_config(cfg))
    pts = {(b.marker_index, b.is_start): round(b.output_time * 90_000) for b in boundaries}
    xml_path = tmp_path / "markers.xml"
    generate_xml(boundaries, pts, xml_path)
    from xml.etree import ElementTree as ET
    descriptors = ET.parse(xml_path).findall('.//splice_segmentation_descriptor')
    starts = [d for d in descriptors if d.get('segmentation_event_id') == '0x00000021']
    assert starts[0].get('sub_segment_num') == '3'
    assert starts[1].get('sub_segment_num') == '0'
    assert starts[1].get('sub_segments_expected') == '0'
    sidecar = build_markers(boundaries, pts)
    assert next(m for m in sidecar if m['event_id'] == '0x00000021' and m['is_out'])['sub_segment_num'] == 3


def test_2019_ad_numbering_differs_from_2023_and_blocks_are_rejected():
    raw = _nested_break_config()
    raw['scte35_numbering_scheme'] = 'SCTE35_2019A'
    cfg = Config.model_validate(raw)
    numbers = {m.event_id: (m.segmentation.segment_num, m.segmentation.segments_expected,
                            m.segmentation.sub_segment_num) for m in cfg.markers}
    assert numbers[101] == (0, 0, 1)
    assert numbers[102] == (0, 0, 2)
    assert numbers[103] == (1, 2, None)
    assert numbers[104] == (2, 2, None)
    raw['markers'][3]['segmentation']['sub_segment_num'] = 1
    with pytest.raises(ValidationError, match='cannot specify sub_segment'):
        Config.model_validate(raw)
    del raw['markers'][3]['segmentation']['sub_segment_num']
    raw['markers'].append({"event_id": 105, "splice_type": "time_signal", "segmentation": {"type_id": "0x44"}, "assets": ["ad1"]})
    with pytest.raises(ValidationError, match='Ad Block'):
        Config.model_validate(raw)


def test_af2m_jingles_and_spots_and_profile_validation():
    raw = {
        'output': {'file': 'out.ts'}, 'scte35_numbering_scheme': 'AF2M_SNPTV',
        'assets': [{'file': f'a{i}.mp4', 'id': f'a{i}', 'role': role}
                   for i, role in enumerate(('jingle', 'advert', 'advert', 'jingle'))],
        'markers': [
            {'event_id': 1, 'splice_type': 'time_signal', 'segmentation': {'type_id': '0x22'}, 'assets': [f'a{i}' for i in range(4)]},
            {'event_id': 2, 'splice_type': 'time_signal', 'segmentation': {'type_id': '0x34'}, 'assets': [f'a{i}' for i in range(4)]},
            *({'event_id': 3 + i, 'splice_type': 'time_signal', 'segmentation': {'type_id': '0x30'},
               'assets': [f'a{i}']} for i in range(4)),
        ],
    }
    cfg = Config.model_validate(raw)
    assert [(m.segmentation.segment_num, m.segmentation.segments_expected, m.segmentation.sub_segment_num)
            for m in cfg.markers] == [(1, 1, None), (1, 1, None), (0, 2, None), (1, 2, None), (2, 2, None), (0, 0, None)]
    raw['markers'][3]['segmentation']['type_id'] = '0x32'
    with pytest.raises(ValidationError, match='unsupported by AF2M_SNPTV'):
        Config.model_validate(raw)
    raw['markers'][3]['segmentation']['type_id'] = '0x30'
    raw['markers'][3]['segmentation']['sub_segment_num'] = 1
    with pytest.raises(ValidationError, match='cannot specify sub_segment'):
        Config.model_validate(raw)


@pytest.mark.parametrize(
    ('playlist', 'expected'),
    [
        ('break-and-ppos-short', {100: (1, 1), 101: (1, 1), 102: (0, 2), 103: (1, 2), 104: (2, 2)}),
        ('break-and-ppos', {
            100: (1, 1), 101: (1, 1), 102: (0, 3), 103: (1, 3), 104: (2, 3), 105: (3, 3),
            200: (1, 1), 201: (1, 1), 202: (0, 5),
            203: (1, 5), 204: (2, 5), 205: (3, 5), 206: (4, 5), 207: (5, 5),
        }),
    ],
)
def test_af2m_example_playlists_number_jingles_separately_from_spots(playlist, expected):
    path = Path(__file__).resolve().parents[2] / 'data' / 'playlists' / f'{playlist}.yaml'
    config = load_config(path)
    assert config.scte35_numbering_scheme == 'AF2M_SNPTV'
    assert {
        marker.event_id: (marker.segmentation.segment_num, marker.segmentation.segments_expected)
        for marker in config.markers if marker.event_id in expected
    } == expected


def test_switching_numbering_scheme_preserves_asset_role():
    raw = {
        'output': {'file': 'out.ts'}, 'scte35_numbering_scheme': 'AF2M_SNPTV',
        'assets': [{'file': f'a{i}.mp4', 'id': f'a{i}', **({'role': 'jingle'} if i == 0 else {})} for i in range(3)],
        'markers': [
            {'event_id': 1, 'splice_type': 'time_signal', 'segmentation': {'type_id': '0x22'}, 'assets': ['a0', 'a1', 'a2']},
            {'event_id': 2, 'splice_type': 'time_signal', 'segmentation': {'type_id': '0x30'}, 'assets': ['a0']},
            {'event_id': 3, 'splice_type': 'time_signal', 'segmentation': {'type_id': '0x30'}, 'assets': ['a1']},
            {'event_id': 4, 'splice_type': 'time_signal', 'segmentation': {'type_id': '0x30'}, 'assets': ['a2']},
        ],
    }
    af2m = Config.model_validate(raw)
    assert (af2m.markers[1].segmentation.segment_num, af2m.markers[1].segmentation.segments_expected) == (0, 2)

    raw['scte35_numbering_scheme'] = 'SCTE35_2023R1'
    scte = Config.model_validate(raw)
    assert scte.assets[0].role == 'jingle'
    assert (scte.markers[1].segmentation.segment_num, scte.markers[1].segmentation.sub_segment_num) == (0, 1)

    raw['scte35_numbering_scheme'] = 'AF2M_SNPTV'
    restored = Config.model_validate(raw)
    assert (restored.markers[1].segmentation.segment_num, restored.markers[1].segmentation.segments_expected) == (0, 2)


def test_relaxed_numbering_preserves_authored_four_fields():
    raw = _nested_break_config()
    raw['enforce_scte35_marker_semantics'] = False
    raw['markers'][3]['segmentation'].update(segment_num=7, segments_expected=9,
                                             sub_segment_num=2, sub_segments_expected=4)
    seg = Config.model_validate(raw).markers[3].segmentation
    assert (seg.segment_num, seg.segments_expected, seg.sub_segment_num, seg.sub_segments_expected) == (7, 9, 2, 4)


def test_break_numbering_without_program_resets_by_provider_interval():
    raw = {
        'output': {'file': 'out.ts'}, 'break_numbering_supported': True,
        'assets': [{'file': f'a{i}.mp4', 'id': f'a{i}'} for i in range(4)],
        'markers': [
            {'event_id': i + 1, 'splice_type': 'time_signal', 'segmentation': {'type_id': '0x22'},
             'break_interval': 1 if i < 2 else 2, 'assets': [f'a{i}']} for i in range(4)
        ],
    }
    cfg = Config.model_validate(raw)
    assert [(m.segmentation.segment_num, m.segmentation.segments_expected) for m in cfg.markers] == [
        (1, 2), (2, 2), (1, 2), (2, 2)
    ]


def test_af2m_jingle_must_be_at_break_edge():
    raw = {
        'output': {'file': 'out.ts'}, 'scte35_numbering_scheme': 'AF2M_SNPTV',
        'assets': [{'file': f'a{i}.mp4', 'id': f'a{i}', **({'role': 'jingle'} if i == 1 else {})} for i in range(3)],
        'markers': [
            {'event_id': 1, 'splice_type': 'time_signal', 'segmentation': {'type_id': '0x22'}, 'assets': ['a0', 'a1', 'a2']},
            {'event_id': 2, 'splice_type': 'time_signal', 'segmentation': {'type_id': '0x30'}, 'assets': ['a0']},
            {'event_id': 3, 'splice_type': 'time_signal', 'segmentation': {'type_id': '0x30'}, 'assets': ['a1']},
            {'event_id': 4, 'splice_type': 'time_signal', 'segmentation': {'type_id': '0x30'}, 'assets': ['a2']},
        ],
    }
    with pytest.raises(ValidationError, match='must be at the start or end'):
        Config.model_validate(raw)


def test_af2m_closing_jingle_uses_asset_role_and_is_excluded_from_spot_count():
    raw = {
        'output': {'file': 'out.ts'}, 'scte35_numbering_scheme': 'AF2M_SNPTV',
        'assets': [{'file': f'a{i}.mp4', 'id': f'a{i}', **({'role': 'jingle'} if i == 2 else {})} for i in range(3)],
        'markers': [
            {'event_id': 1, 'splice_type': 'time_signal', 'segmentation': {'type_id': '0x22'}, 'assets': ['a0', 'a1', 'a2']},
            *({'event_id': i + 2, 'splice_type': 'time_signal', 'segmentation': {'type_id': '0x30'}, 'assets': [f'a{i}']}
              for i in range(3)),
        ],
    }
    cfg = Config.model_validate(raw)
    assert [(m.segmentation.segment_num, m.segmentation.segments_expected) for m in cfg.markers] == [
        (1, 1), (1, 2), (2, 2), (0, 0),
    ]


def test_legacy_marker_jingle_role_migrates_to_asset():
    raw = {
        'output': {'file': 'out.ts'}, 'scte35_numbering_scheme': 'AF2M_SNPTV',
        'assets': [{'file': f'a{i}.mp4', 'id': f'a{i}'} for i in range(2)],
        'markers': [
            {'event_id': 1, 'splice_type': 'time_signal', 'segmentation': {'type_id': '0x22'}, 'assets': ['a0', 'a1']},
            {'event_id': 2, 'splice_type': 'time_signal', 'segmentation': {'type_id': '0x30'}, 'jingle_role': 'opening', 'assets': ['a0']},
            {'event_id': 3, 'splice_type': 'time_signal', 'segmentation': {'type_id': '0x30'}, 'assets': ['a1']},
        ],
    }
    cfg = Config.model_validate(raw)
    assert cfg.assets[0].role == 'jingle'
    assert not hasattr(cfg.markers[1], 'jingle_role')
    assert (cfg.markers[1].segmentation.segment_num, cfg.markers[1].segmentation.segments_expected) == (0, 1)


def test_legacy_asset_jingle_roles_migrate_to_general_role():
    raw = {
        'output': {'file': 'out.ts'},
        'assets': [{'file': 'intro.mp4', 'id': 'intro', 'jingle_role': 'opening'},
                   {'file': 'outro.mp4', 'id': 'outro', 'jingle_role': 'closing'}],
    }
    cfg = Config.model_validate(raw)
    assert [asset.role for asset in cfg.assets] == ['jingle', 'jingle']


def test_af2m_role_can_be_set_before_pad_and_advert_has_no_extra_semantics():
    raw = {
        'output': {'file': 'out.ts'}, 'scte35_numbering_scheme': 'AF2M_SNPTV',
        'assets': [{'file': 'intro.mp4', 'id': 'intro', 'role': 'jingle'},
                   {'file': 'ad.mp4', 'id': 'ad', 'role': 'advert'},
                   {'file': 'outro.mp4', 'id': 'outro', 'role': 'jingle'}],
        'markers': [{'event_id': 1, 'splice_type': 'time_signal', 'segmentation': {'type_id': '0x22'},
                     'assets': ['intro', 'ad', 'outro']}],
    }
    assert Config.model_validate(raw).markers[0].segmentation.segment_num == 1
    raw['markers'] += [
        {'event_id': i + 2, 'splice_type': 'time_signal', 'segmentation': {'type_id': '0x30'}, 'assets': [asset]}
        for i, asset in enumerate(('intro', 'ad', 'outro'))
    ]
    cfg = Config.model_validate(raw)
    assert [(marker.segmentation.segment_num, marker.segmentation.segments_expected)
            for marker in cfg.markers] == [(1, 1), (0, 1), (1, 1), (0, 0)]


def test_instant_marker_scte35_emission_single_message():
    raw = {
        "output": {"file": "out.ts"},
        "assets": [
            {"file": "content1.mp4", "id": "c1"},
            {"file": "ad1.mp4", "id": "ad1"},
        ],
        "markers": [
            {"event_id": 1, "splice_type": "time_signal",
             "segmentation": {"type_id": "0x12", "upid_hex": "aa"}, "assets": ["ad1"]},
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
    assert descs[0].get("segmentation_type_id") == "0x12"


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
