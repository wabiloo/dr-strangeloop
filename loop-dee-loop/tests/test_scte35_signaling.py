"""Tests for scte35_signaling.py's HLS EXT-X-DATERANGE authoring."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scte35_signaling import (  # noqa: E402
    SCTE35_EVENT_ID_MAX,
    SignalingMarker,
    build_cue_breaks,
    build_cue_in_tag,
    build_cue_out_cont_tag,
    build_cue_out_tag,
    build_daterange_tags,
    build_event_id_map,
    build_grouped_daterange_tags,
    compute_event_id_step,
    compute_incremented_event_id,
    group_markers_by_event_id,
    is_instant_segmentation,
    is_out_marker,
    reencode_event_ids,
    resolve_marker_duration_ticks,
)

_START = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)


def test_daterange_id_is_segtype_event_loop_decimal():
    """ID format is `<segmentation_type_id>-<event_id>-<loop_number>`, all
    decimal (e.g. 0x22 Break Start + event 0x64 + loop 3 -> "34-100-3")."""
    marker = SignalingMarker(
        event_id="0x00000064",
        pts_time_ticks=0,
        segmentation_type_id="0x22",
        segmentation_duration_ticks=None,
        splice_command_b64="AAAA",
        is_out=True,
    )

    tags = build_daterange_tags([marker], timescale=90_000, program_start_datetime=_START, loop_number=3)

    assert 'ID="34-100-3"' in tags[0]


def test_duplicate_id_marker_identity_disambiguates_hls_id_and_groups_stop():
    out = {
        "event_id": "0x00000001", "marker_identity": "10", "splice_type": "splice_insert",
        "pts_time_ticks": 0, "is_out": True,
    }
    stop = {
        "event_id": "0x00000001", "marker_identity": "10", "splice_type": "splice_insert",
        "pts_time_ticks": 900_000, "is_out": False,
    }
    markers = [
        SignalingMarker("0x00000001", 0, None, None, "AAAA", True, marker_identity="10"),
        SignalingMarker("0x00000001", 1, None, None, "BBBB", False, marker_identity="11"),
    ]
    tags = build_daterange_tags(markers, 90_000, _START)
    assert 'ID="splice-out-1-0-10"' in tags[0]
    assert 'ID="splice-in-1-0-11"' in tags[1]
    assert resolve_marker_duration_ticks(out, {("0x00000001", "10"): [out, stop]}) == 900_000


def test_daterange_id_uses_splice_out_prefix_when_no_segmentation():
    marker = SignalingMarker(
        event_id="0x00000001",
        pts_time_ticks=0,
        segmentation_type_id=None,
        segmentation_duration_ticks=None,
        splice_command_b64="AAAA",
        is_out=True,
    )

    tags = build_daterange_tags([marker], timescale=90_000, program_start_datetime=_START, loop_number=0)

    assert 'ID="splice-out-1-0"' in tags[0]


def test_daterange_id_differs_for_splice_insert_out_vs_in():
    """A splice_insert cue-out and its explicit (non-auto_return) cue-in
    land in the same loop iteration with the same event_id -- with no
    segmentation_type_id parity to lean on (unlike time_signal), their IDs
    must still differ, or hls.js treats the second tag as an update to the
    same DateRange (whose START-DATE then mismatches per RFC 8216 4.3.2.7)
    instead of a second, independently-timed cue -- the cue-in's own
    cuechange activation silently never fires."""
    out_marker = SignalingMarker(
        event_id="0x00000001",
        pts_time_ticks=0,
        segmentation_type_id=None,
        segmentation_duration_ticks=450_000,
        splice_command_b64="AAAA",
        is_out=True,
    )
    in_marker = SignalingMarker(
        event_id="0x00000001",
        pts_time_ticks=450_000,
        segmentation_type_id=None,
        segmentation_duration_ticks=None,
        splice_command_b64="BBBB",
        is_out=False,
    )

    tags = build_daterange_tags(
        [out_marker, in_marker], timescale=90_000, program_start_datetime=_START, loop_number=0,
    )

    assert 'ID="splice-out-1-0"' in tags[0]
    assert 'ID="splice-in-1-0"' in tags[1]


def test_instant_marker_has_no_planned_duration():
    import base64

    marker = SignalingMarker(
        event_id="0x00000069",
        pts_time_ticks=0,
        segmentation_type_id="0x02",
        segmentation_duration_ticks=450_000,
        splice_command_b64=base64.b64encode(b"\x01\x02").decode(),
        is_out=True,
        is_instant=True,
    )

    tags = build_daterange_tags([marker], timescale=90_000, program_start_datetime=_START, loop_number=0)
    tag = tags[0]

    assert "PLANNED-DURATION" not in tag
    assert "SCTE35-OUT" not in tag
    assert "SCTE35-IN" not in tag
    assert "SCTE35-CMD=0x0102" in tag


def test_non_instant_out_marker_still_gets_planned_duration_and_out():
    import base64

    marker = SignalingMarker(
        event_id="0x00000064",
        pts_time_ticks=0,
        segmentation_type_id="0x22",
        segmentation_duration_ticks=2_700_000,
        splice_command_b64=base64.b64encode(b"\x01\x02").decode(),
        is_out=True,
        is_instant=False,
    )

    tags = build_daterange_tags([marker], timescale=90_000, program_start_datetime=_START, loop_number=0)
    tag = tags[0]

    assert "PLANNED-DURATION=30.000" in tag
    assert "SCTE35-OUT=0x0102" in tag


def test_is_instant_segmentation_recognizes_call_ad_server():
    assert is_instant_segmentation({"segmentation_type_id": "0x02"}) is True
    assert is_instant_segmentation({"segmentation_type_id": "0x30"}) is False
    assert is_instant_segmentation({"segmentation_type_id": None}) is False


def test_explicit_out_flag_overrides_type_id_for_nonconsecutive_program_pairs():
    assert is_out_marker({"segmentation_type_id": "0x17", "is_out": True}) is True
    assert is_out_marker({"segmentation_type_id": "0x11", "is_out": False}) is False
    assert is_instant_segmentation({"segmentation_type_id": "0x11", "is_instant": False}) is False


def test_legacy_table_23_program_end_is_treated_as_end():
    assert is_out_marker({"segmentation_type_id": "0x11"}) is False
    assert is_out_marker({"segmentation_type_id": "0x14"}) is False


def test_call_ad_server_is_out_marker_true_but_instant_overrides_in_serve():
    """0x02 is even, so `is_out_marker` alone would call it a CUE-OUT --
    callers must additionally check `is_instant_segmentation` (see
    serve.py's `_marker_covers_segment`) to avoid treating it as an
    open-ended interval."""
    assert is_out_marker({"segmentation_type_id": "0x02"}) is True
    assert is_instant_segmentation({"segmentation_type_id": "0x02"}) is True


# ── [markers].daterange_mode = "grouped" ─────────────────────────────────


def test_grouped_daterange_collapses_coincident_markers_into_one_tag():
    """A Break start (0x22) + nested PPO start (0x30), coincident at the
    same PTS, collapse into one tag under daterange_mode='grouped' --
    always SCTE35-CMD, never OUT/IN + PLANNED-DURATION (no single member
    correctly describes the group's direction/duration as a whole)."""
    import base64

    break_start = SignalingMarker(
        event_id="0x00000064",
        pts_time_ticks=0,
        segmentation_type_id="0x22",
        segmentation_duration_ticks=2_700_000,
        splice_command_b64=base64.b64encode(b"\x01\x02").decode(),
        is_out=True,
    )
    ppo_start = SignalingMarker(
        event_id="0x00000065",
        pts_time_ticks=0,
        segmentation_type_id="0x30",
        segmentation_duration_ticks=900_000,
        splice_command_b64=base64.b64encode(b"\x01\x02").decode(),
        is_out=True,
    )

    tags = build_grouped_daterange_tags(
        [break_start, ppo_start], timescale=90_000, program_start_datetime=_START, loop_number=0,
    )

    assert len(tags) == 1
    assert 'ID="group-100-0"' in tags[0]  # min(0x64, 0x65) = 100 decimal
    assert "SCTE35-CMD=0x0102" in tags[0]
    assert "PLANNED-DURATION" not in tags[0]
    assert "SCTE35-OUT" not in tags[0]


def test_grouped_daterange_single_member_group_falls_back_to_ungrouped_id():
    marker = SignalingMarker(
        event_id="0x00000064",
        pts_time_ticks=0,
        segmentation_type_id="0x22",
        segmentation_duration_ticks=None,
        splice_command_b64="AAAA",
        is_out=True,
    )

    grouped = build_grouped_daterange_tags(
        [marker], timescale=90_000, program_start_datetime=_START, loop_number=0,
    )
    ungrouped = build_daterange_tags(
        [marker], timescale=90_000, program_start_datetime=_START, loop_number=0,
    )

    assert grouped == ungrouped


def test_grouped_daterange_merges_instant_marker_coincident_with_others():
    """An instant signal (e.g. 0x02 Call Ad Server) riding along in the
    same physical message as a Break start at the same PTS -- a real,
    observed combination (see PlaybackPanel.vue) -- gets folded into the
    SAME group tag, not split into a second tag of its own."""
    import base64

    instant = SignalingMarker(
        event_id="0x00000069",
        pts_time_ticks=0,
        segmentation_type_id="0x02",
        segmentation_duration_ticks=None,
        splice_command_b64=base64.b64encode(b"\x01\x02").decode(),
        is_out=True,
        is_instant=True,
    )
    break_start = SignalingMarker(
        event_id="0x00000064",
        pts_time_ticks=0,
        segmentation_type_id="0x22",
        segmentation_duration_ticks=900_000,
        splice_command_b64=base64.b64encode(b"\x01\x02").decode(),
        is_out=True,
    )

    tags = build_grouped_daterange_tags(
        [instant, break_start], timescale=90_000, program_start_datetime=_START, loop_number=0,
    )

    assert len(tags) == 1
    assert 'ID="group-100-0"' in tags[0]  # min(0x64, 0x69) = 100 decimal
    assert "SCTE35-CMD=0x0102" in tags[0]


# ── [markers].cue_tags = "alongside" | "only" ────────────────────────────


def test_resolve_duration_prefers_segmentation_duration_field():
    marker = {"event_id": "0x1", "segmentation_duration_ticks": 900_000, "is_out": True}
    assert resolve_marker_duration_ticks(marker, {}) == 900_000


def test_resolve_duration_falls_back_to_paired_stop_marker_for_splice_insert():
    out = {"event_id": "0x1", "pts_time_ticks": 0, "splice_type": "splice_insert", "is_out": True}
    stop = {"event_id": "0x1", "pts_time_ticks": 900_000, "splice_type": "splice_insert", "is_out": False}

    grouped = group_markers_by_event_id([out, stop])

    assert resolve_marker_duration_ticks(out, grouped) == 900_000


def test_resolve_duration_returns_none_for_in_marker_or_unpaired_out():
    in_marker = {"event_id": "0x1", "pts_time_ticks": 0, "splice_type": "splice_insert", "is_out": False}
    assert resolve_marker_duration_ticks(in_marker, {}) is None

    unpaired_out = {"event_id": "0x1", "pts_time_ticks": 0, "splice_type": "splice_insert", "is_out": True}
    assert resolve_marker_duration_ticks(unpaired_out, group_markers_by_event_id([unpaired_out])) is None


def _time_signal_marker(event_id, seg_type_id, pts, duration, is_out):
    return {
        "event_id": event_id,
        "splice_type": "time_signal",
        "segmentation_type_id": seg_type_id,
        "pts_time_ticks": pts,
        "segmentation_duration_ticks": duration,
        "is_out": is_out,
    }


def _splice_insert(event_id, pts, is_out):
    return {"event_id": event_id, "splice_type": "splice_insert", "pts_time_ticks": pts, "is_out": is_out}


def test_build_cue_breaks_ignores_time_signal_markers():
    """Regression test: a real time_signal source can have several
    coincident-but-differently-durationed segmentation types active at
    once (e.g. a 30s Break containing a 5s PPO containing a 10s Ad) --
    build_cue_breaks must ignore all of them (splice_insert-only), or
    each becomes its own independent CUE-OUT/-CONT/-IN sequence and a
    single segment ends up under several simultaneously-open avails with
    different DURATIONs."""
    break_start = _time_signal_marker("0x64", "0x22", 0, 2_700_000, True)  # 30s Break
    break_end = _time_signal_marker("0x64", "0x23", 2_700_000, None, False)
    ppo_start = _time_signal_marker("0x65", "0x34", 0, 450_000, True)  # 5s PPO
    ppo_end = _time_signal_marker("0x65", "0x35", 450_000, None, False)

    assert build_cue_breaks([break_start, break_end, ppo_start, ppo_end]) == []


def test_build_cue_breaks_only_from_splice_insert_even_when_mixed():
    break_start = _time_signal_marker("0x64", "0x22", 0, 2_700_000, True)
    break_end = _time_signal_marker("0x64", "0x23", 2_700_000, None, False)
    ad_out = _splice_insert("0x01", 900_000, True)
    ad_in = _splice_insert("0x01", 1_800_000, False)

    breaks = build_cue_breaks([break_start, break_end, ad_out, ad_in])

    assert len(breaks) == 1
    assert breaks[0]["event_id"] == "0x01"
    assert breaks[0]["start_ticks"] == 900_000
    assert breaks[0]["end_ticks"] == 1_800_000
    assert breaks[0]["duration_ticks"] == 900_000


def test_cue_tag_formats():
    assert build_cue_out_tag(2_700_000, 90_000) == "#EXT-X-CUE-OUT:DURATION=30.000"
    assert build_cue_in_tag() == "#EXT-X-CUE-IN"
    assert (
        build_cue_out_cont_tag(900_000, 2_700_000, 90_000)
        == "#EXT-X-CUE-OUT-CONT:ELAPSED-TIME=10.000,DURATION=30.000"
    )


# ── [markers].increment_event_ids ────────────────────────────────────────


def test_compute_event_id_step_is_next_power_of_ten_above_the_max():
    # base ids 100 (0x64) .. 190 (0xBE) -> step 1000, matching the worked
    # example in its-a-live/AGENTS.md.
    assert compute_event_id_step(["0x00000064", "0x000000BE"]) == 1000
    assert compute_event_id_step(["0x00000001", "0x00000002"]) == 10
    assert compute_event_id_step(["0x000003E8"]) == 10_000  # exactly 1000 -> next decade up
    assert compute_event_id_step([]) == 10


def test_compute_incremented_event_id_uses_step_per_loop():
    assert compute_incremented_event_id("0x00000064", 0, step=1000) == "0x00000064"
    assert compute_incremented_event_id("0x00000064", 1, step=1000) == f"0x{100 + 1000:08X}"
    assert compute_incremented_event_id("0x00000064", 2, step=1000) == f"0x{100 + 2000:08X}"
    assert compute_incremented_event_id("0x000000BE", 3, step=1000) == f"0x{190 + 3000:08X}"


def test_compute_incremented_event_id_wraps_loop_number_back_to_zero_at_ceiling():
    base = SCTE35_EVENT_ID_MAX - 1  # max_loop_number = 1 // 1 = 1 -> cycles base, base+1, base, ...
    assert compute_incremented_event_id(f"0x{base:08X}", 1, step=1) == f"0x{base + 1:08X}"
    assert compute_incremented_event_id(f"0x{base:08X}", 2, step=1) == f"0x{base:08X}"
    assert compute_incremented_event_id(f"0x{base:08X}", 3, step=1) == f"0x{base + 1:08X}"


def test_build_event_id_map_derives_one_shared_step_from_every_marker():
    markers = [{"event_id": "0x00000064"}, {"event_id": "0x000000BE"}]  # 100, 190 -> step 1000
    assert build_event_id_map(markers, 1) == {
        "0x00000064": f"0x{100 + 1000:08X}",
        "0x000000BE": f"0x{190 + 1000:08X}",
    }
    assert build_event_id_map(markers, 2) == {
        "0x00000064": f"0x{100 + 2000:08X}",
        "0x000000BE": f"0x{190 + 2000:08X}",
    }


# A real splice_insert message (command_type 5) with an explicit
# splice_event_id on the command itself -- from a well-known public SCTE-35
# test vector, verified in prototyping to decode via threefive without a
# franken-ts/.ts round trip (see this file's own module docstring: these
# tests use fabricated/known data, not real .ts decoding).
_SPLICE_INSERT_B64 = "/DAvAAAAAAAA///wFAVIAACPf+/+c2nALv4AUsz1AAAAAAAKAAhDVUVJAAABNWLbowo="


def test_reencode_event_ids_round_trips_command_splice_event_id():
    threefive = pytest.importorskip("threefive")

    original = threefive.Cue(_SPLICE_INSERT_B64)
    original.decode()
    original_id = original.command.splice_event_id
    original_key = f"0x{original_id:08X}"
    new_key = f"0x{original_id + 5:08X}"

    reencoded_b64 = reencode_event_ids(_SPLICE_INSERT_B64, {original_key: new_key})
    assert reencoded_b64 != _SPLICE_INSERT_B64

    reencoded = threefive.Cue(reencoded_b64)
    reencoded.decode()  # raises/produces garbage if the CRC is wrong
    assert reencoded.command.splice_event_id == original_id + 5


def test_reencode_event_ids_is_a_noop_when_nothing_matches():
    result = reencode_event_ids(_SPLICE_INSERT_B64, {"0xDEADBEEF": "0xCAFEBABE"})
    assert result == _SPLICE_INSERT_B64


# A real time_signal message with three coincident segmentation
# descriptors (event ids 0x64, 0x66, 0x69), captured from an actual bake
# (break-and-ppos-short fixture). Distinct from _SPLICE_INSERT_B64 above:
# threefive decodes `segmentation_event_id` as a hex STRING ('0x64', not
# zero-padded) rather than an int like `command.splice_event_id` -- a
# real bug (crash: "Unknown format code 'X' for object of type 'str'")
# was caught only by exercising this shape, not the splice_insert one.
_TIME_SIGNAL_MULTI_DESCRIPTOR_B64 = (
    "/DB/AAAAAAAAAP/wBQb+ABt3QABpAAhDVUVJAAAAEgIaQ1VFSQAAAGR/0QAAKTLgCQZCUkVBSzEiAAECG0NVRUkAAABmf9EA"
    "AAbd0AkHSklOR0xFMTAAAwIkQ1VFSQAAAGl/0QAABt3QDBBBREZSATPxATSwTwZeBgIgAgEDgOVGUQ=="
)


def test_reencode_event_ids_round_trips_segmentation_event_id():
    """Regression test for a crash: threefive's segmentation_event_id is
    a hex string, and assigning an int back to it (as `reencode_event_ids`
    used to) silently corrupts the descriptor -- `.encode()` then raises
    deep inside a subsequent internal decode() with an unrelated-looking
    error, rather than failing where the bad assignment happened."""
    threefive = pytest.importorskip("threefive")

    original = threefive.Cue(_TIME_SIGNAL_MULTI_DESCRIPTOR_B64)
    original.decode()
    seg_event_ids = [
        getattr(d, "segmentation_event_id", None) for d in original.descriptors
    ]
    seg_event_ids = [eid for eid in seg_event_ids if eid is not None]
    assert len(seg_event_ids) == 3
    assert all(isinstance(eid, str) for eid in seg_event_ids)  # confirms the shape under test

    # Keys must be the zero-padded "0x%08X" form reencode_event_ids itself
    # normalizes to before looking up -- not the raw, unpadded decoded
    # string ('0x64'), which wouldn't match.
    id_map = {f"0x{int(eid, 16):08X}": f"0x{int(eid, 16) + 100:08X}" for eid in seg_event_ids}
    reencoded_b64 = reencode_event_ids(_TIME_SIGNAL_MULTI_DESCRIPTOR_B64, id_map)
    assert reencoded_b64 != _TIME_SIGNAL_MULTI_DESCRIPTOR_B64

    reencoded = threefive.Cue(reencoded_b64)
    reencoded.decode()  # raises/produces garbage if a descriptor got corrupted
    new_ids = {
        int(eid, 16)
        for eid in (getattr(d, "segmentation_event_id", None) for d in reencoded.descriptors)
        if eid is not None
    }
    assert new_ids == {int(eid, 16) + 100 for eid in seg_event_ids}
