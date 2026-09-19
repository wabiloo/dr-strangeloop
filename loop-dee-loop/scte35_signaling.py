"""Author HLS EXT-X-DATERANGE and DASH EventStream/Event SCTE-35 signaling
directly from `.markers.json` (SCOPE.md §4.1 step 5).

Do NOT rely on GPAC's own scte35dec EventStream/DATERANGE aggregation — it
was confirmed unreliable for multi-marker real content in prototyping (see
SCOPE.md §6): only one <Event> was produced for a 4-marker file, with a
presentationTime off by 2 seconds. This module reads `.markers.json`
directly and authors the signaling ourselves, so every marker is guaranteed
present at the correct offset.

All ticks handled here are *loop-relative* (0 .. total_loop_duration_ticks),
per SCOPE.md §4.1 step 5 — absolute wall-clock shifting happens later, once
per request, in serve.py.
"""

from __future__ import annotations

import base64
import datetime as _dt
from dataclasses import dataclass
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

DEFAULT_TIMESCALE = 90_000


@dataclass(frozen=True)
class SignalingMarker:
    """One SCTE-35 marker, ready to be rendered into HLS/DASH signaling.

    `pts_time_ticks` must already be loop-relative (bake-time value, in the
    channel timescale) by the time it reaches this module.
    """

    event_id: str  # e.g. "0x00000001"
    pts_time_ticks: int
    segmentation_type_id: str | None
    segmentation_duration_ticks: int | None
    splice_command_b64: str
    is_out: bool  # True = CUE-OUT / splice-out, False = CUE-IN / splice-in


def _splice_command_base64_from_marker(marker: dict) -> str:
    """Return the marker's original SCTE-35 splice command as base64.

    `.markers.json` (SCOPE.md §2) does not itself carry the raw splice_info_section
    bytes -- only the decoded fields used to reconstruct it. If the marker
    dict already carries a `splice_command_b64` (e.g. injected by a future
    franken-ts version, or reconstructed by bake.py step 1 from the verified
    threefive decode of the real embedded message), that value is trusted
    and returned as-is. This keeps this module decoupled from *how* the
    caller obtained the base64 payload (re-encoding decoded fields vs.
    capturing the original bytes at validation time in bake.py).
    """
    if "splice_command_b64" in marker:
        return marker["splice_command_b64"]
    raise KeyError(
        "marker is missing 'splice_command_b64' -- bake.py's validation step "
        "must attach the original base64 SCTE-35 message to each marker "
        "before calling into scte35_signaling"
    )


def is_out_marker(m: dict) -> bool:
    """Whether a raw `.markers.json` entry is a CUE-OUT (ad-break start) as
    opposed to a CUE-IN (ad-break end). Shared between `markers_to_signaling`
    (HLS/DASH tag authoring) and serve.py's marker/segment-overlap placement
    logic, so both agree on which markers carry a real, forward-looking
    active interval (see serve.py's `_marker_covers_segment`) versus which
    are a single point-in-time signal.

    Even `segmentation_type_id` -> "start" (e.g. 0x34 Program Start, 0x30
    Distributor placement opportunity start), odd -> "end" pair, matching
    franken-ts's own start/start+1 convention (scte35.py).
    """
    splice_type = m.get("splice_type")
    seg_type_id = m.get("segmentation_type_id")
    if seg_type_id is not None:
        type_id_int = int(seg_type_id, 16) if isinstance(seg_type_id, str) else seg_type_id
        return (type_id_int % 2) == 0
    if splice_type == "splice_insert":
        # splice_insert markers don't carry a segmentation_type_id in
        # markers.json; caller must set an explicit "is_out" flag instead.
        return bool(m.get("is_out", True))
    return True


def markers_to_signaling(
    markers: list[dict],
    timescale: int = DEFAULT_TIMESCALE,
) -> list[SignalingMarker]:
    """Convert raw `.markers.json` entries (with attached base64 splice
    commands) into the intermediate `SignalingMarker` representation shared
    by both the HLS and DASH authoring functions below."""
    result = []
    for m in markers:
        seg_type_id = m.get("segmentation_type_id")
        is_out = is_out_marker(m)

        result.append(
            SignalingMarker(
                event_id=m["event_id"],
                pts_time_ticks=m["pts_time_ticks"],
                segmentation_type_id=seg_type_id,
                segmentation_duration_ticks=m.get("segmentation_duration_ticks"),
                splice_command_b64=_splice_command_base64_from_marker(m),
                is_out=is_out,
            )
        )
    return result


def _iso8601_duration_seconds(ticks: int, timescale: int) -> float:
    return ticks / timescale


def build_daterange_tags(
    markers: list[SignalingMarker],
    timescale: int,
    program_start_datetime: _dt.datetime,
    loop_number: int = 0,
) -> list[str]:
    """Return a list of `#EXT-X-DATERANGE:...` tag lines, one per marker.

    `program_start_datetime` is the wall-clock time corresponding to
    loop-relative tick 0 for *this* manifest response (i.e. already shifted
    by `loop_number * total_loop_duration_ticks` -- see serve.py). This
    function performs the tick->ISO8601 conversion exactly once per marker,
    as a final display step (SCOPE.md §4.2 "Hard rule").

    Follows the standard SCTE-35-in-HLS mapping: CUE-OUT markers get
    SCTE35-OUT + PLANNED-DURATION, CUE-IN markers get SCTE35-IN, both also
    carry SCTE35-CMD with the raw splice command.

    `loop_number` is folded into the emitted DATERANGE `ID` (e.g.
    "0x00000001" -> "0x00000001-loop3"). Per RFC 8216 §4.4.5.1, an `ID`
    that reappears across playlist reloads must carry byte-for-byte
    identical attributes every time -- but a looping channel legitimately
    re-signals the *same* underlying `event_id` every iteration with a new
    START-DATE (real wall-clock time), which would otherwise violate that
    rule. hls.js/most compliant clients detect the mismatch and silently
    drop the tag (observed: "DATERANGE tag attribute: START-DATE does not
    match for tags with ID..."), so downstream ad-signaling silently stops
    working after the first loop. Scoping the ID to the loop number gives
    each occurrence a genuinely unique, internally-consistent ID.
    """
    tags: list[str] = []
    for marker in markers:
        offset_seconds = _iso8601_duration_seconds(marker.pts_time_ticks, timescale)
        start_date = program_start_datetime + _dt.timedelta(seconds=offset_seconds)
        start_date_str = start_date.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        attrs = [
            f'ID="{marker.event_id}-loop{loop_number}"',
            f'START-DATE="{start_date_str}"',
            'CLASS="com.scte35"',
        ]

        if marker.segmentation_duration_ticks is not None:
            duration_seconds = _iso8601_duration_seconds(
                marker.segmentation_duration_ticks, timescale
            )
            attrs.append(f'PLANNED-DURATION={duration_seconds:.3f}')

        attrs.append(f'SCTE35-CMD=0x{_b64_to_hex(marker.splice_command_b64)}')
        if marker.is_out:
            attrs.append(f'SCTE35-OUT=0x{_b64_to_hex(marker.splice_command_b64)}')
        else:
            attrs.append(f'SCTE35-IN=0x{_b64_to_hex(marker.splice_command_b64)}')

        tags.append("#EXT-X-DATERANGE:" + ",".join(attrs))

    return tags


def _b64_to_hex(b64_value: str) -> str:
    return base64.b64decode(b64_value).hex().upper()


def build_eventstream_xml(
    markers: list[SignalingMarker],
    timescale: int,
) -> str:
    """Return a serialized `<EventStream>` element (loop-relative ticks),
    to be embedded directly into the DASH MPD's video AdaptationSet.

    Shape confirmed working end-to-end in prototyping (SCOPE.md §4.1 step 5):

        <EventStream schemeIdUri="urn:scte:scte35:2014:xml+bin" timescale="90000">
          <Event presentationTime="..." duration="...">
            <Signal xmlns="urn:scte:scte35:2013:xml">
              <Binary>...base64 splice command...</Binary>
            </Signal>
          </Event>
        </EventStream>
    """
    event_stream = ET.Element(
        "EventStream",
        {
            "schemeIdUri": "urn:scte:scte35:2014:xml+bin",
            "timescale": str(timescale),
        },
    )

    for marker in markers:
        event_attrs = {"presentationTime": str(marker.pts_time_ticks)}
        if marker.segmentation_duration_ticks is not None:
            event_attrs["duration"] = str(marker.segmentation_duration_ticks)
        event_attrs["id"] = marker.event_id

        event = ET.SubElement(event_stream, "Event", event_attrs)
        signal = ET.SubElement(event, "Signal", {"xmlns": "urn:scte:scte35:2013:xml"})
        binary = ET.SubElement(signal, "Binary")
        binary.text = marker.splice_command_b64

    ET.indent(event_stream, space="  ")
    return ET.tostring(event_stream, encoding="unicode")


def loop_relative_ticks(markers: list[dict]) -> list[dict]:
    """Return a shallow copy of markers, guaranteeing `pts_time_ticks` is
    treated as loop-relative (0..total_loop_duration_ticks). This is mostly
    a documentation/typing seam: bake.py is responsible for ensuring
    markers.json's own ticks are already loop-relative (they are, by
    construction, since franken-ts's output starts at tick/time 0)."""
    return [dict(m) for m in markers]
