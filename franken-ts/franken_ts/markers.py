from __future__ import annotations

import json
import logging
from pathlib import Path

from .pts import PTS_CLOCK
from .timeline import AdBoundary

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

    for boundary in boundaries:
        ab = boundary.ad_break
        pts = pts_map[(boundary.event_id, boundary.is_start)]

        entry: dict = {
            "event_id": f"0x{boundary.event_id:08X}",
            "splice_type": ab.splice_type,
            "pts_time_ticks": pts,
            "pts_time_seconds": pts / PTS_CLOCK,
        }

        if ab.splice_type == "time_signal" and ab.segmentation is not None:
            seg = ab.segmentation
            start_type_id = (
                int(seg.type_id, 16) if isinstance(seg.type_id, str) else seg.type_id
            )
            type_id = start_type_id if boundary.is_start else start_type_id + 1
            seg_duration_seconds = seg.duration_seconds()
            seg_duration_ticks = (
                round(seg_duration_seconds * PTS_CLOCK)
                if seg_duration_seconds is not None
                else round(boundary.break_duration * PTS_CLOCK)
            )

            entry.update(
                {
                    "segmentation_type_id": f"0x{type_id:02X}",
                    "segmentation_duration_ticks": seg_duration_ticks,
                    "upid_type": seg.upid_type,
                    "upid_hex": seg.upid_hex,
                    "flags": _flags_for(seg),
                }
            )

        markers.append(entry)

    return markers


def write_markers_sidecar(
    boundaries: list[AdBoundary],
    pts_map: dict[tuple[int, bool], int],
    markers_path: Path,
    *,
    dry_run: bool = False,
) -> None:
    """Write the `<output>.markers.json` sidecar (see SCOPE.md §2 of loop-packager).

    Always written (not gated behind --debug/--verify) so downstream tools
    (loop-packager) have a single source of truth for marker timing.
    """
    markers = build_markers(boundaries, pts_map)

    if dry_run:
        print(f"[dry-run] would write {len(markers)} marker(s) → {markers_path}")
        return

    with markers_path.open("w", encoding="utf-8") as f:
        json.dump(markers, f, indent=2)
        f.write("\n")

    logger.info("Wrote markers JSON to %s (%d events)", markers_path, len(markers))
