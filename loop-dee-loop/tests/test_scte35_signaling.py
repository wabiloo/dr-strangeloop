"""Tests for scte35_signaling.py's HLS EXT-X-DATERANGE authoring."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scte35_signaling import (  # noqa: E402
    SignalingMarker,
    build_daterange_tags,
    is_instant_segmentation,
    is_out_marker,
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


def test_daterange_id_uses_zero_type_id_when_no_segmentation():
    marker = SignalingMarker(
        event_id="0x00000001",
        pts_time_ticks=0,
        segmentation_type_id=None,
        segmentation_duration_ticks=None,
        splice_command_b64="AAAA",
        is_out=True,
    )

    tags = build_daterange_tags([marker], timescale=90_000, program_start_datetime=_START, loop_number=0)

    assert 'ID="0-1-0"' in tags[0]


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


def test_call_ad_server_is_out_marker_true_but_instant_overrides_in_serve():
    """0x02 is even, so `is_out_marker` alone would call it a CUE-OUT --
    callers must additionally check `is_instant_segmentation` (see
    serve.py's `_marker_covers_segment`) to avoid treating it as an
    open-ended interval."""
    assert is_out_marker({"segmentation_type_id": "0x02"}) is True
    assert is_instant_segmentation({"segmentation_type_id": "0x02"}) is True
