from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional
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
    end_pts: Optional[int],
) -> None:
    """Emit the splice-out message, and -- unless `ab.auto_return` -- the
    paired splice-in message. `end_pts` is None exactly when `auto_return`
    is set (no stop boundary was resolved for this marker -- see
    `timeline.resolve_markers`)."""
    ab = boundary_start.marker
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
                  auto_return=str(ab.auto_return).lower(),
                  duration=format_pts(break_duration_pts))
    if ab.descriptors:
        _avail_descriptor(sit_out, ab.provider_avail_id)

    if ab.auto_return:
        return

    # Splice-in (end of ad break)
    assert end_pts is not None
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
    if ab.descriptors:
        _avail_descriptor(sit_in, ab.provider_avail_id)


def _time_signal_message(
    root: ET.Element,
    boundaries_at_pts: list[AdBoundary],
    pts_map: dict[tuple[int, bool], int],
    pts: int,
) -> None:
    """Emit ONE `time_signal` splice_information_table at `pts`, containing one
    `splice_segmentation_descriptor` per boundary that resolves to this PTS.

    This is how nested markers (e.g. BreakStart + first child's Start, both
    at the same instant) get combined into a single SCTE-35 message instead
    of one message per event -- the standard way multiple simultaneous
    segmentation events are signaled.
    """
    sit = ET.SubElement(root, "splice_information_table",
                         protocol_version="0",
                         pts_adjustment="0",
                         tier="0x0FFF")
    ET.SubElement(sit, "time_signal", pts_time=format_pts(pts))

    for boundary in boundaries_at_pts:
        ab = boundary.marker
        seg = ab.segmentation
        event_hex = f"0x{boundary.event_id:08X}"

        break_duration_pts = round(boundary.break_duration * PTS_CLOCK)
        seg_duration_pts = (
            round(seg.duration_seconds() * PTS_CLOCK)
            if seg.duration_seconds() is not None
            else break_duration_pts
        )

        # Start/stop type_ids are consecutive pairs in SCTE-35 Table 22, e.g.
        # 0x34 Program Start / 0x35 Program End, 0x38 Break Start / 0x39 Break
        # End. The stop is always start + 1.
        start_type_id = int(seg.type_id, 16) if isinstance(seg.type_id, str) else seg.type_id
        stop_type_id = start_type_id + 1
        type_id = start_type_id if boundary.is_start else stop_type_id

        seg_desc = ET.SubElement(sit, "splice_segmentation_descriptor",
                                 segmentation_event_id=event_hex,
                                 web_delivery_allowed=str(seg.web_delivery_allowed).lower(),
                                 no_regional_blackout=str(seg.no_regional_blackout).lower(),
                                 archive_allowed=str(seg.archive_allowed).lower(),
                                 device_restrictions=str(seg.device_restrictions),
                                 segmentation_duration=format_pts(seg_duration_pts),
                                 segmentation_type_id=f"0x{type_id:02X}",
                                 segment_num=str(seg.segment_num or 0),
                                 segments_expected=str(seg.segments_expected or 0),
                                 sub_segment_num="0",
                                 sub_segments_expected="0")
        upid = ET.SubElement(seg_desc, "segmentation_upid", type=seg.upid_type)
        upid.text = seg.upid_hex


def generate_xml(
    boundaries: list[AdBoundary],
    pts_map: dict[tuple[int, bool], int],
    xml_path: Path,
) -> None:
    """Generate the tsduck SCTE-35 XML file."""
    root = ET.Element("tsduck")

    seen: set[int] = set()

    # `splice_insert` boundaries: one message pair per event, exactly as
    # before -- splice_insert has no natural way to combine multiple events
    # into a single message, so no merging is needed/possible here.
    for boundary in boundaries:
        if boundary.marker.splice_type != "splice_insert":
            continue
        if not boundary.is_start:
            continue
        eid = boundary.event_id
        if eid in seen:
            continue
        seen.add(eid)

        start_pts = pts_map[(eid, True)]
        end_pts = pts_map.get((eid, False))  # None when auto_return (no stop boundary)

        root.append(ET.Comment(f" Event {eid} "))
        _splice_insert_pair(root, boundary, start_pts, end_pts)

    # `time_signal` boundaries: group by resolved PTS so that coincident
    # markers (e.g. BreakStart + PPOStart + nested AdStart, all at the same
    # instant) emit ONE message with multiple descriptors instead of one
    # message per event.
    by_pts: dict[int, list[AdBoundary]] = {}
    for boundary in boundaries:
        if boundary.marker.splice_type != "time_signal":
            continue
        pts = pts_map[(boundary.event_id, boundary.is_start)]
        by_pts.setdefault(pts, []).append(boundary)
        seen.add(boundary.event_id)

    for pts in sorted(by_pts):
        boundaries_at_pts = by_pts[pts]
        ids = ", ".join(str(b.event_id) for b in boundaries_at_pts)
        root.append(ET.Comment(f" Events {ids} @ pts {pts} "))
        _time_signal_message(root, boundaries_at_pts, pts_map, pts)

    ET.indent(root, space="    ")
    tree = ET.ElementTree(root)

    with xml_path.open("wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)

    logger.info("Wrote SCTE-35 XML to %s (%d events)", xml_path, len(seen))
