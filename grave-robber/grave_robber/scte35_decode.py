"""Unified SCTE-35 binary decode (SCOPE.md §5.2) -- funnels every marker,
regardless of source format/encoding, through `threefive`, matching
loop-dee-loop bake.py's own posture (`decode_embedded_scte35`): the real
position always comes from where the message was actually OBSERVED (there:
demuxed PES PTS; here: the manifest-derived position from extract_hls.py/
extract_dash.py), never the splice command's own internal `pts_time` field.

`threefive.Cue` natively accepts base64, hex, AND raw XML (both the
DASH-IF `xml+bin` `<Signal><Binary>...</Binary></Signal>` wrapper and a
bare `<SpliceInfoSection>` document) -- so the DASH XML-native case needs
no separate re-encoding step of our own; `mpd-inspector` is only ever used
for XML *navigation* (finding the right element), never for SCTE-35
binary/segmentation semantics, which stays threefive's job exclusively.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

import threefive

from .models import RawMarker

TIMESCALE = 90_000


def _normalize_event_id(event_id) -> str:
    """threefive sometimes returns an event id as an int, sometimes as a
    hex string -- normalize to markers.json's fixed "0x%08X" form (mirrors
    loop-dee-loop bake.py's decode_embedded_scte35 callback)."""
    if isinstance(event_id, str) and event_id.startswith("0x"):
        return f"0x{int(event_id, 16):08X}"
    return f"0x{int(event_id):08X}"


def _normalize_type_id(type_id) -> str | None:
    if type_id is None:
        return None
    if isinstance(type_id, str):
        return f"0x{int(type_id, 16):02X}"
    return f"0x{int(type_id):02X}"


def decode_marker(raw: RawMarker) -> dict:
    """Decode one RawMarker into a markers.json-shaped dict (loop-dee-loop/
    SCOPE.md §2), ready to feed straight into loop-dee-loop's bake.py
    sparse segment-list input mode (§11) -- no `.ts` exists for an
    archive-derived source, so there is no
    decode_embedded_scte35/validate_markers_against_ts cross-check step;
    this dict is trusted as-is, the same way franken-ts's own output is.
    """
    if raw.splice_command_xml is not None:
        xml_str = ET.tostring(raw.splice_command_xml, encoding="unicode")
        cue = threefive.Cue(xml_str)
    elif raw.splice_command_b64 is not None:
        cue = threefive.Cue(raw.splice_command_b64)
    else:
        raise ValueError(
            f"RawMarker (source={raw.source!r}, pts_time_ticks={raw.pts_time_ticks}) "
            f"has neither splice_command_b64 nor splice_command_xml -- nothing to decode."
        )
    cue.decode()

    # Always re-derive true base64 from the decoded cue, regardless of the
    # raw form actually captured (hex is common on the wire for HLS
    # SCTE35-OUT/-CMD) -- DASH's <Binary> element requires real base64, and
    # loop-dee-loop's scte35_signaling.py trusts this field verbatim.
    splice_command_b64 = cue.base64()

    command = cue.command
    seg_descriptors = [
        d for d in cue.descriptors if getattr(d, "segmentation_event_id", None) is not None
    ]

    result: dict = {
        "source": raw.source,
        "pts_time_ticks": raw.pts_time_ticks,
        "pts_time_seconds": raw.pts_time_ticks / TIMESCALE,
        "splice_command_b64": splice_command_b64,
    }

    if seg_descriptors:
        # time_signal (or a segmentation-descriptor-carrying splice_insert):
        # event identity + duration/UPID/flags all live on the descriptor,
        # never the bare splice command -- same as loop-dee-loop bake.py's
        # own decode_embedded_scte35.
        descriptor = seg_descriptors[0]
        result["event_id"] = _normalize_event_id(descriptor.segmentation_event_id)
        result["splice_type"] = "time_signal"
        result["segmentation_type_id"] = _normalize_type_id(descriptor.segmentation_type_id)
        if getattr(descriptor, "segmentation_duration", None) is not None:
            result["segmentation_duration_ticks"] = round(
                descriptor.segmentation_duration * TIMESCALE
            )
        upid_type = getattr(descriptor, "segmentation_upid_type", None)
        if upid_type is not None:
            result["upid_type"] = _normalize_type_id(upid_type)
        upid = getattr(descriptor, "segmentation_upid", None)
        if upid is not None:
            result["upid_hex"] = upid[2:] if isinstance(upid, str) and upid.startswith("0x") else upid
        result["flags"] = {
            "web_delivery_allowed": bool(getattr(descriptor, "web_delivery_allowed_flag", False)),
            "no_regional_blackout": bool(getattr(descriptor, "no_regional_blackout_flag", False)),
            "archive_allowed": bool(getattr(descriptor, "archive_allowed_flag", False)),
        }
    else:
        # splice_insert with no segmentation descriptor -- event identity +
        # out/in direction + duration live directly on the command.
        event_id = getattr(command, "splice_event_id", None)
        if event_id is None:
            raise ValueError(
                f"Decoded SCTE-35 message (source={raw.source!r}) has no event_id "
                f"on either a segmentation descriptor or the splice command itself."
            )
        result["event_id"] = _normalize_event_id(event_id)
        result["splice_type"] = "splice_insert"
        # A splice_insert's own out/in direction is explicit and known here
        # at decode time (out_of_network_indicator) -- set it rather than
        # letting scte35_signaling.is_out_marker's splice_insert fallback
        # (defaults True when unset) potentially get an IN half wrong.
        result["is_out"] = bool(getattr(command, "out_of_network_indicator", True))
        break_duration = getattr(command, "break_duration", None)
        if break_duration is not None:
            result["segmentation_duration_ticks"] = round(break_duration * TIMESCALE)

    return result
