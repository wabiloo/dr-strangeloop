"""Tests for scte35_decode.py (SCOPE.md §5.2) -- both the b64/hex case and
the DASH XML-native case, funneled through the same `threefive` decode."""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grave_robber.models import RawMarker  # noqa: E402
from grave_robber.scte35_decode import decode_marker  # noqa: E402

# time_signal + segmentation descriptor (Provider Placement Opportunity End,
# type_id 0x35), event_id 0x4800008E -- a real, valid SCTE-35 message.
TIME_SIGNAL_B64 = "/DAvAAAAAAAA///wBQb+dGKQoAAZAhdDVUVJSAAAjn+fCAgAAAAALKChijUCAKnMZ1g="

# splice_insert, out_of_network_indicator=True, break_duration=60.293567s,
# splice_event_id=1207959695 -- a real, valid SCTE-35 message.
SPLICE_INSERT_OUT_B64 = "/DAvAAAAAAAA///wFAVIAACPf+/+c2nALv4AUsz1AAAAAAAKAAhDVUVJAAABNWLbowo="


def test_decode_time_signal_daterange_marker():
    raw = RawMarker(source="daterange", pts_time_ticks=540_000, splice_command_b64=TIME_SIGNAL_B64)

    decoded = decode_marker(raw)

    assert decoded["event_id"] == "0x4800008E"
    assert decoded["splice_type"] == "time_signal"
    assert decoded["pts_time_ticks"] == 540_000  # manifest-derived position preserved, not the cue's own
    assert decoded["segmentation_type_id"] == "0x35"
    assert decoded["upid_type"] == "0x08"
    assert decoded["splice_command_b64"] == TIME_SIGNAL_B64
    assert decoded["flags"]["web_delivery_allowed"] is True


def test_decode_never_trusts_the_cues_own_internal_pts_time():
    """The splice command's own pts_time (21695.74s) must never leak into
    the result -- the manifest-derived position always wins (SCOPE.md §5.2:
    mirrors bake.py's decode_embedded_scte35 treatment)."""
    raw = RawMarker(source="daterange", pts_time_ticks=123, splice_command_b64=TIME_SIGNAL_B64)

    decoded = decode_marker(raw)

    assert decoded["pts_time_ticks"] == 123


def test_decode_splice_insert_out_sets_is_out_and_duration():
    raw = RawMarker(source="cue-out", pts_time_ticks=90_000, splice_command_b64=SPLICE_INSERT_OUT_B64)

    decoded = decode_marker(raw)

    assert decoded["event_id"] == "0x4800008F"  # 1207959695 in hex
    assert decoded["splice_type"] == "splice_insert"
    assert decoded["is_out"] is True
    assert decoded["segmentation_duration_ticks"] == round(60.293567 * 90_000)


def test_decode_accepts_hex_form_payload():
    import base64

    hex_payload = "0x" + base64.b64decode(TIME_SIGNAL_B64).hex().upper()
    raw = RawMarker(source="cue-out", pts_time_ticks=0, splice_command_b64=hex_payload)

    decoded = decode_marker(raw)

    assert decoded["event_id"] == "0x4800008E"
    # Always re-normalized to real base64 regardless of the wire form
    # captured (DASH's <Binary> needs real base64) -- see decode_marker's
    # docstring.
    assert decoded["splice_command_b64"] == TIME_SIGNAL_B64


def test_decode_xml_native_eventstream_marker():
    xml_str = f"""<Event presentationTime="180000" duration="2700000" id="1207959695">
  <Signal xmlns="urn:scte:scte35:2013:xml">
    <Binary>{TIME_SIGNAL_B64}</Binary>
  </Signal>
</Event>"""
    element = ET.fromstring(xml_str)
    raw = RawMarker(source="eventstream-bin", pts_time_ticks=180_000, splice_command_xml=element)

    decoded = decode_marker(raw)

    assert decoded["event_id"] == "0x4800008E"
    assert decoded["splice_command_b64"] == TIME_SIGNAL_B64


def test_decode_raises_when_marker_has_neither_form():
    raw = RawMarker(source="daterange", pts_time_ticks=0)

    with pytest.raises(ValueError, match="nothing to decode"):
        decode_marker(raw)
