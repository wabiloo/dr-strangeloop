"""SCTE-35 marker helpers shared by the HLS, DASH and window-JSON builders: which markers a segment
carries, how they are described, and how their event ids are remapped."""

from __future__ import annotations

import dataclasses

from scte35_signaling import (
    SignalingMarker,
    _segmentation_code,
    build_event_id_map,
    is_instant_segmentation,
    is_out_marker,
    markers_to_signaling,
    reencode_event_ids,
)


def _marker_type_code(marker: dict) -> str:
    """The marker's compact SCTE-35 code with direction suffix, as used for
    DATERANGE ids (`PPOs` = Provider Placement Opportunity Start, `PPOe` =
    End; `SPIs`/`SPIe` for a bare splice_insert)."""
    return _segmentation_code(markers_to_signaling([marker])[0])


def _describe_markers(entries: list[tuple[dict, dict[str, str], int | None, str]]) -> list[str]:
    """Comment lines summarizing markers: one line per distinct start time
    (ISO 8601, UTC) -- coincident markers share a line -- each listing the
    markers' compact type codes and decimal event ids (loop-remapped, with the
    original, if `increment_event_ids`). Entries are (marker, event-id map,
    loop number or None to omit it, start time)."""
    groups: dict[tuple[str, int | None], list[str]] = {}
    for marker, id_map, loop, start_iso in entries:
        event_id = int(id_map.get(marker["event_id"], marker["event_id"]), 16)
        detail = str(event_id)
        # With increment_event_ids the map is non-empty: show the original id too.
        if id_map:
            detail += f", orig {int(marker['event_id'], 16)}"
        groups.setdefault((start_iso, loop), []).append(f"{_marker_type_code(marker)} ({detail})")
    return [
        f"markers @ {start_iso}{'' if loop is None else f' (loop {loop})'}: {', '.join(parts)}"
        for (start_iso, loop), parts in groups.items()
    ]


def _dash_event_comment(entry: tuple[dict, dict[str, str], int | None, str]) -> str:
    """`_describe_markers` for one marker, as an indented XML comment line
    placed right before the <Event> it describes (newline-ended)."""
    return "".join(f"    <!-- {line} -->\n" for line in _describe_markers([entry]))


def _hls_payload_markers(markers: list[dict], daterange_mode: str) -> list[dict]:
    """The markers as HLS should carry them: with `narrowed` descriptor
    grouping each marker's own single-descriptor payload replaces the shared
    whole-message one (bake.py stores both, so this is a per-request choice)."""
    if daterange_mode != "narrowed":
        return markers
    return [
        {**m, "splice_command_b64": m["splice_command_b64_narrowed"]} if m.get("splice_command_b64_narrowed") else m
        for m in markers
    ]


def _marker_covers_segment(
    marker: dict, seg_start_ticks: int, seg_end_ticks: int, announce_ticks: int = 0
) -> bool:
    """Whether `marker` should be signaled (EXT-X-DATERANGE / DASH <Event>)
    on a segment spanning [seg_start_ticks, seg_end_ticks).

    A CUE-OUT marker with a `segmentation_duration_ticks` represents an
    active *interval* -- [pts_time_ticks, pts_time_ticks + duration) -- not
    just its own single starting instant. It must keep being signaled on
    every segment that interval overlaps, for as long as any such segment
    remains in the DVR window; otherwise a player that joins mid-break, or
    whose playlist/manifest reload lands after the break's *first* segment
    has already scrolled out of the window, never sees any signal that
    it's mid-ad-break at all, even though later segments of that same
    break are still being served.

    A CUE-IN marker (or any marker with no duration) is instead a single
    point-in-time signal -- it only ever belongs to the one segment whose
    window contains its own `pts_time_ticks`, regardless of any duration
    field it happens to carry (that field describes the segmentation
    interval that just elapsed, not an instruction to keep signaling
    forward from here).

    Standalone/instant markers (`is_instant_segmentation`, e.g. 0x02 Call
    Ad Server) are always point-in-time too, even though their even
    `segmentation_type_id` would otherwise satisfy `is_out_marker` -- there
    is no matching CUE-IN that will ever close them, so they must never be
    treated as an open forward-looking interval.

    `announce_ticks` > 0 announces the marker that long BEFORE its own start:
    its coverage then begins at `pts_time_ticks - announce_ticks` and runs
    through the same end as above (the end of its active interval, or for a
    point-in-time marker the segment containing its start). So a marker
    announced ahead stays covered -- and therefore in the manifest -- until
    every segment it covers has left the window, not only until the segment
    it was first announced on has. Only the placement changes: the marker's
    own time (START-DATE, Event@presentationTime) does not.
    """
    start = marker["pts_time_ticks"]
    announce = start - announce_ticks
    duration = marker.get("segmentation_duration_ticks")
    if duration and is_out_marker(marker) and not is_instant_segmentation(marker):
        end = start + duration
        return seg_start_ticks < end and announce < seg_end_ticks
    return announce < seg_end_ticks and seg_start_ticks <= start


def _marker_type_id(marker: dict) -> int | None:
    """The marker's segmentation_type_id as an int, None for a plain splice_insert."""
    raw = marker.get("segmentation_type_id")
    if raw is None:
        return None
    return int(raw, 16) if isinstance(raw, str) else int(raw)


def _marker_durations_ticks(markers: list[dict]) -> list[int]:
    """Per-marker active duration in ticks (0 = point-in-time): the marker's
    own `segmentation_duration_ticks` for an OUT, else the distance to the
    matching IN (same event id / identity, later PTS) -- how a splice_insert
    break, which carries no duration of its own, gets one."""
    out = []
    for m in markers:
        if not is_out_marker(m) or is_instant_segmentation(m):
            out.append(0)
            continue
        own = int(m.get("segmentation_duration_ticks") or 0)
        if own:
            out.append(own)
            continue
        ends = [
            o["pts_time_ticks"]
            for o in markers
            if o["event_id"] == m["event_id"]
            and o.get("marker_identity") == m.get("marker_identity")
            and not is_out_marker(o)
            and o["pts_time_ticks"] > m["pts_time_ticks"]
        ]
        out.append(min(ends) - m["pts_time_ticks"] if ends else 0)
    return out


def _asset_ids_starting_in_segment(
    asset_boundaries: list[dict], seg_start_ticks: int, seg_end_ticks: int
) -> list[str]:
    """Asset ids (in timeline order) whose `start_ticks` falls inside
    [seg_start_ticks, seg_end_ticks) -- i.e. every new asset that begins
    somewhere within this one segment. Unlike marker placement, this is a
    plain point-in-interval test: an asset boundary is a single instant
    (where the new asset's content starts), never a signaled interval."""
    return [
        b["asset_id"] for b in asset_boundaries
        if seg_start_ticks <= b["start_ticks"] < seg_end_ticks
    ]


def _remap_signaling_markers(
    signaling_markers: list[SignalingMarker], all_markers: list[dict], loop_number: int
) -> list[SignalingMarker]:
    """[markers].increment_event_ids: return `signaling_markers` with their
    `event_id`/`splice_command_b64` remapped to this loop iteration's
    incremented ids (see scte35_signaling.compute_incremented_event_id).

    The id map is built from `all_markers` (every marker in the package),
    not just `signaling_markers` (the ones being rendered right now) --
    a shared multi-descriptor SCTE-35 message can carry OTHER markers'
    event ids too (see bake.py's decode_embedded_scte35 docstring), and
    those must be remapped in lockstep for the re-encoded bytes to stay
    internally consistent regardless of which marker triggered rendering.
    """
    id_map = build_event_id_map(all_markers, loop_number)
    return [
        dataclasses.replace(
            m,
            event_id=id_map[m.event_id],
            splice_command_b64=reencode_event_ids(m.splice_command_b64, id_map),
        )
        for m in signaling_markers
    ]
