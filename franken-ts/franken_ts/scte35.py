from __future__ import annotations

import logging
from pathlib import Path
from xml.etree import ElementTree as ET

from .pts import PTS_CLOCK
from .timeline import AdBoundary
from .utils import format_pts

logger = logging.getLogger(__name__)


def _avail_descriptor(parent: ET.Element, provider_avail_id: str) -> None:
    ET.SubElement(parent, "splice_avail_descriptor",
                  identifier="0x43554549",
                  provider_avail_id=provider_avail_id)


def _splice_insert_pair(
    root: ET.Element,
    boundary_start: AdBoundary,
    start_pts: int,
    end_pts: int,
) -> None:
    ab = boundary_start.ad_break
    break_duration_pts = round(boundary_start.break_duration * PTS_CLOCK)
    event_hex = f"0x{ab.event_id:08X}"

    # Splice-out (start of ad break)
    sit_out = ET.SubElement(root, "splice_information_table",
                            protocol_version="0",
                            pts_adjustment="0",
                            tier="0x0FFF")
    si_out = ET.SubElement(sit_out, "splice_insert",
                           splice_event_id=event_hex,
                           splice_event_cancel="false",
                           out_of_network="true",
                           splice_immediate="false",
                           unique_program_id=ab.unique_program_id,
                           avail_num=str(ab.avail_num),
                           avails_expected=str(ab.avails_expected),
                           pts_time=format_pts(start_pts))
    ET.SubElement(si_out, "break_duration",
                  auto_return="false",
                  duration=format_pts(break_duration_pts))
    _avail_descriptor(sit_out, ab.provider_avail_id)

    # Splice-in (end of ad break)
    sit_in = ET.SubElement(root, "splice_information_table",
                           protocol_version="0",
                           pts_adjustment="0",
                           tier="0x0FFF")
    ET.SubElement(sit_in, "splice_insert",
                  splice_event_id=event_hex,
                  splice_event_cancel="false",
                  out_of_network="false",
                  splice_immediate="false",
                  unique_program_id=ab.unique_program_id,
                  avail_num=str(ab.avail_num),
                  avails_expected=str(ab.avails_expected),
                  pts_time=format_pts(end_pts))
    _avail_descriptor(sit_in, ab.provider_avail_id)


def _time_signal_pair(
    root: ET.Element,
    boundary_start: AdBoundary,
    start_pts: int,
    end_pts: int,
) -> None:
    ab = boundary_start.ad_break
    seg = ab.segmentation
    event_hex = f"0x{ab.event_id:08X}"

    break_duration_pts = round(boundary_start.break_duration * PTS_CLOCK)
    seg_duration_pts = (
        round(seg.duration_seconds() * PTS_CLOCK)
        if seg.duration_seconds() is not None
        else break_duration_pts
    )

    # The start and stop type_ids are defined as consecutive pairs in SCTE-35
    # Table 23: e.g. 0x34 Program Start / 0x35 Program End, 0x38 Break Start /
    # 0x39 Break End.  The stop is always start + 1.
    start_type_id = int(seg.type_id, 16) if isinstance(seg.type_id, str) else seg.type_id
    stop_type_id  = start_type_id + 1

    # ── Time signal start ─────────────────────────────────────────────────────
    sit_start = ET.SubElement(root, "splice_information_table",
                              protocol_version="0",
                              pts_adjustment="0",
                              tier="0x0FFF")
    ET.SubElement(sit_start, "time_signal", pts_time=format_pts(start_pts))
    _avail_descriptor(sit_start, ab.provider_avail_id)

    seg_desc_start = ET.SubElement(sit_start, "splice_segmentation_descriptor",
                             segmentation_event_id=event_hex,
                             web_delivery_allowed=str(seg.web_delivery_allowed).lower(),
                             no_regional_blackout=str(seg.no_regional_blackout).lower(),
                             archive_allowed=str(seg.archive_allowed).lower(),
                             device_restrictions=str(seg.device_restrictions),
                             segmentation_duration=format_pts(seg_duration_pts),
                             segmentation_type_id=seg.type_id,
                             segment_num="0",
                             segments_expected="0",
                             sub_segment_num="0",
                             sub_segments_expected="0")
    upid_start = ET.SubElement(seg_desc_start, "segmentation_upid", type=seg.upid_type)
    upid_start.text = seg.upid_hex

    # ── Time signal stop ──────────────────────────────────────────────────────
    sit_stop = ET.SubElement(root, "splice_information_table",
                             protocol_version="0",
                             pts_adjustment="0",
                             tier="0x0FFF")
    ET.SubElement(sit_stop, "time_signal", pts_time=format_pts(end_pts))
    _avail_descriptor(sit_stop, ab.provider_avail_id)

    seg_desc_stop = ET.SubElement(sit_stop, "splice_segmentation_descriptor",
                             segmentation_event_id=event_hex,
                             web_delivery_allowed=str(seg.web_delivery_allowed).lower(),
                             no_regional_blackout=str(seg.no_regional_blackout).lower(),
                             archive_allowed=str(seg.archive_allowed).lower(),
                             device_restrictions=str(seg.device_restrictions),
                             segmentation_type_id=f"0x{stop_type_id:02X}",
                             segment_num="0",
                             segments_expected="0",
                             sub_segment_num="0",
                             sub_segments_expected="0")
    upid_stop = ET.SubElement(seg_desc_stop, "segmentation_upid", type=seg.upid_type)
    upid_stop.text = seg.upid_hex


def generate_xml(
    boundaries: list[AdBoundary],
    pts_map: dict[tuple[int, bool], int],
    xml_path: Path,
) -> None:
    """Generate the tsduck SCTE-35 XML file."""
    root = ET.Element("tsduck")

    seen: set[int] = set()
    for boundary in boundaries:
        if not boundary.is_start:
            continue
        eid = boundary.event_id
        if eid in seen:
            continue
        seen.add(eid)

        start_pts = pts_map[(eid, True)]
        end_pts = pts_map[(eid, False)]
        ab = boundary.ad_break

        root.append(ET.Comment(f" Event {eid} "))

        if ab.splice_type == "splice_insert":
            _splice_insert_pair(root, boundary, start_pts, end_pts)
        else:
            _time_signal_pair(root, boundary, start_pts, end_pts)

    ET.indent(root, space="    ")
    tree = ET.ElementTree(root)

    with xml_path.open("wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)

    logger.info("Wrote SCTE-35 XML to %s (%d events)", xml_path, len(seen))
