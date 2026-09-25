from __future__ import annotations

import json
import logging
from pathlib import Path

from .pts import PTS_CLOCK
from .config import is_instant_segmentation, is_segmentation_start_type_id
from .timeline import AdBoundary, pts_for_boundary

logger = logging.getLogger(__name__)


def _flags_for(seg) -> dict:
    return {
        "web_delivery_allowed": seg.web_delivery_allowed,
        "no_regional_blackout": seg.no_regional_blackout,
        "archive_allowed": seg.archive_allowed,
        "device_restrictions": seg.device_restrictions,
    }


def build_markers(
    boundaries: list[AdBoundary],
    pts_map: dict[tuple[int, bool], int],
) -> list[dict]:
    """Build the list of marker-event dicts to serialize to `.markers.json`.

    One entry per injected SCTE-35 event (splice-out/start and splice-in/stop
    are both emitted, each as their own event with their own `pts_time_ticks`),
    matching exactly what `scte35.generate_xml` wrote to the tsduck XML.
    """
    markers: list[dict] = []
    duplicate_ids: set[int] = set()
    seen_ids: set[int] = set()
    for boundary in boundaries:
        if boundary.is_start:
            if boundary.event_id in seen_ids:
                duplicate_ids.add(boundary.event_id)
            seen_ids.add(boundary.event_id)

    for boundary in boundaries:
        ab = boundary.marker
        pts = pts_for_boundary(pts_map, boundary)
        if pts is None:
            raise KeyError((boundary.marker_index, boundary.is_start))

        entry: dict = {
            "event_id": f"0x{boundary.event_id:08X}",
            "type": ab.type,
            "splice_type": ab.splice_type,
            "pts_time_ticks": pts,
            "pts_time_seconds": pts / PTS_CLOCK,
            "assets": list(ab.assets),
            # loop-dee-loop uses explicit role rather than inferring from the
            # segmentation ID (some Table 23 pairs are non-consecutive).
            "is_out": (
                boundary.is_start
                if ab.splice_type == "splice_insert"
                else is_segmentation_start_type_id(boundary.segmentation_type_id)
                if boundary.segmentation_type_id is not None
                else boundary.is_start
            ),
        }
        if boundary.event_id in duplicate_ids:
            entry["marker_identity"] = boundary.marker_index

        if ab.splice_type == "time_signal" and ab.segmentation is not None:
            seg = ab.segmentation
            type_id = boundary.segmentation_type_id
            if type_id is None:
                raise ValueError(f"time_signal boundary {boundary.event_id} is missing its segmentation type ID")
            is_instant = is_instant_segmentation(seg) and boundary.is_start
            seg_duration_seconds = seg.duration_seconds()
            seg_duration_ticks = (
                0 if not boundary.is_start else (
                    round(seg_duration_seconds * PTS_CLOCK)
                    if seg_duration_seconds is not None
                    else round(boundary.break_duration * PTS_CLOCK)
                )
            )

            entry.update(
                {
                    "segmentation_type_id": f"0x{type_id:02X}",
                    "is_instant": is_instant,
                    "segmentation_duration_ticks": seg_duration_ticks,
                    "segment_num": seg.segment_num or 0,
                    "segments_expected": seg.segments_expected or 0,
                    "upid_type": seg.upid_type,
                    "upid_hex": seg.upid_hex,
                    "flags": _flags_for(seg),
                }
            )
            if boundary.is_start and seg.sub_segment_num is not None:
                entry["sub_segment_num"] = seg.sub_segment_num
                entry["sub_segments_expected"] = seg.sub_segments_expected or 0

        markers.append(entry)

    return markers


def write_markers_sidecar(
    boundaries: list[AdBoundary],
    pts_map: dict[tuple[int, bool], int],
    markers_path: Path,
    *,
    dry_run: bool = False,
) -> None:
    """Write the `<output>.markers.json` sidecar (see SCOPE.md §2 of loop-dee-loop).

    Always written (not gated behind --debug/--verify) so downstream tools
    (loop-dee-loop) have a single source of truth for marker timing.
    """
    markers = build_markers(boundaries, pts_map)

    if dry_run:
        print(f"[dry-run] would write {len(markers)} marker(s) → {markers_path}")
        return

    with markers_path.open("w", encoding="utf-8") as f:
        json.dump(markers, f, indent=2)
        f.write("\n")

    logger.info("Wrote markers JSON to %s (%d events)", markers_path, len(markers))
