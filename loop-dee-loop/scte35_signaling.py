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
import string
import unicodedata
from dataclasses import dataclass
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from scte35_table23_data import (
    INSTANT_SEGMENTATION_TYPE_IDS,
    SEGMENTATION_END_TYPE_ID,
    SEGMENTATION_TYPE_CODE,
    SEGMENTATION_TYPE_NAME,
)

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
    is_instant: bool = False  # True = standalone signal, no OUT/IN pairing
    marker_identity: str | None = None


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


# SEGMENTATION_TYPE_NAME/CODE and SEGMENTATION_END_TYPE_ID (Table 23
# reference data, including which segmentation_type_id values are
# standalone/instant -- INSTANT_SEGMENTATION_TYPE_IDS, e.g. 0x02 "Call Ad
# Server") come from the `scte35_table23` workspace package, shared with
# franken-ts's own `franken_ts.config`. loop-dee-loop is deployed as flat
# files into a standalone Docker image (see loop-dee-loop/Dockerfile), so
# it can't depend on that sibling package at runtime -- this module
# instead imports a generated, checked-in copy,
# `loop-dee-loop/scte35_table23_data.py` (see that file's header and
# `scripts/generate_scte35_tables.py`). `tests/test_scte35_tables_sync.py`
# fails if it's ever regenerated and not committed.
#
# The int-keyed views below are just this module's own convenience
# derivations from that shared string/int-keyed data (mirroring how
# franken-ts's config.py derives its own frozensets).
SEGMENTATION_END_TYPE_IDS: frozenset[int] = frozenset(SEGMENTATION_END_TYPE_ID.values())
SEGMENTATION_START_TYPE_IDS: frozenset[int] = frozenset(SEGMENTATION_END_TYPE_ID)
SEGMENTATION_TYPE_CODES: dict[int, str] = {int(k, 16): v for k, v in SEGMENTATION_TYPE_CODE.items()}
SEGMENTATION_TYPE_NAMES: dict[int, str] = {int(k, 16): v for k, v in SEGMENTATION_TYPE_NAME.items()}
# Some End type_ids (e.g. 0x11 Program End) are shared by more than one
# Start (0x10/0x17/0x19): first-Start-wins, same as the table this replaces
# hardcoded, so an ambiguous End consistently falls back to naming/coding
# itself after its numerically-first possible Start.
SEGMENTATION_START_TYPE_FOR_END: dict[int, int] = {}
for _start, _end in SEGMENTATION_END_TYPE_ID.items():
    SEGMENTATION_START_TYPE_FOR_END.setdefault(_end, _start)
del _start, _end

DATERANGE_ID_FORMAT_DEFAULT = "{segcode}-{eventid}-{loop}"
DATERANGE_ID_FIELDS = frozenset({
    "loop", "eventid", "segid", "seghex", "segcode", "segname", "epoch", "pd",
})

DASH_SIGNAL_FORMATS = frozenset({"binary", "xml"})
DASH_SIGNAL_FORMAT_DEFAULT = "binary"
DASH_DESCRIPTOR_MODES = frozenset({"shared", "narrowed"})
DASH_DESCRIPTOR_MODE_DEFAULT = "shared"
SCTE35_XML_NAMESPACE = "urn:scte:scte35:2013:xml"


def validate_daterange_id_format(value: str | None) -> str | None:
    """Validate template fields and reject format conversions/specifiers."""
    if value is None:  # Old baked loop descriptors retain their old ID scheme.
        return value
    if not isinstance(value, str):
        raise ValueError("daterange_id_format must be a string")
    try:
        for _literal, field_name, format_spec, conversion in string.Formatter().parse(value):
            if field_name is None:
                continue
            if field_name not in DATERANGE_ID_FIELDS:
                raise ValueError(
                    f"unknown daterange_id_format placeholder {{{field_name}}}; "
                    f"supported placeholders: {', '.join(sorted(DATERANGE_ID_FIELDS))}"
                )
            if format_spec or conversion:
                raise ValueError(
                    "daterange_id_format placeholders do not support format specs or conversions"
                )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"invalid daterange_id_format: {exc}") from exc
    return value


def validate_dash_signal_format(value: str | None) -> str:
    """[markers].dash_signal_format: shape of the DASH <EventStream>'s
    <Signal> children -- the raw base64 splice command
    (schemeIdUri="...2014:xml+bin", <Binary>, today's only behavior) or
    each marker's full decoded <SpliceInfoSection> per the SCTE-35 XML
    binding (schemeIdUri="...2013:xml"). `None` marks packages baked
    before this setting existed and preserves their "binary" behavior."""
    if value is None:
        return DASH_SIGNAL_FORMAT_DEFAULT
    if value not in DASH_SIGNAL_FORMATS:
        raise ValueError(f"dash_signal_format must be 'binary' or 'xml', got {value!r}")
    return value


def validate_dash_descriptor_mode(value: str | None) -> str:
    """[markers].dash_descriptor_mode: whether each DASH <Event>'s Signal
    payload is the full *shared* multi-descriptor message (default -- same
    bytes for every marker coincident at that PTS, today's only behavior),
    or a *narrowed*, per-event re-encode carrying only that marker's own
    descriptor (see bake.py's `decode_embedded_scte35`). Independent of
    HLS's `daterange_mode` -- a channel can run HLS "shared" and DASH
    "narrowed" (or vice versa) at the same time. `None` marks packages
    baked before this setting existed and preserves their "shared"
    behavior."""
    if value is None:
        return DASH_DESCRIPTOR_MODE_DEFAULT
    if value not in DASH_DESCRIPTOR_MODES:
        raise ValueError(f"dash_descriptor_mode must be 'shared' or 'narrowed', got {value!r}")
    return value


_DEVICE_RESTRICTIONS_CODES = {
    "Restrict Group 0": 0,
    "Restrict Group 1": 1,
    "Restrict Group 2": 2,
    "None": 3,
}


def _xml_ticks(seconds: float) -> int:
    # threefive's own decode normalizes pts/duration fields to seconds
    # (float) -- SCTE-35 stores them as integer 90kHz ticks on the wire,
    # which is also this module's own tick domain (DEFAULT_TIMESCALE),
    # so round-tripping back is just the inverse of that decode step.
    return round(seconds * DEFAULT_TIMESCALE)


def _xml_bool(value: bool) -> str:
    return "true" if value else "false"


def _xml_attrs(pairs: list[tuple[str, object]]) -> str:
    return "".join(f' {key}="{value}"' for key, value in pairs if value is not None)


def _hex_field(value: str | int) -> int:
    return int(value, 16) if isinstance(value, str) else value


def _splice_insert_xml(command) -> str:
    attrs = [
        ("spliceEventId", command.splice_event_id),
        ("spliceEventCancelIndicator", _xml_bool(command.splice_event_cancel_indicator)),
    ]
    if command.splice_event_cancel_indicator:
        return f"<scte35:SpliceInsert{_xml_attrs(attrs)}/>"
    attrs += [
        ("outOfNetworkIndicator", _xml_bool(command.out_of_network_indicator)),
        ("spliceImmediateFlag", _xml_bool(command.splice_immediate_flag)),
        ("eventIdComplianceFlag", _xml_bool(command.event_id_compliance_flag)),
        ("uniqueProgramId", command.unique_program_id),
        ("availNum", command.avail_num),
        ("availsExpected", command.avails_expected),
    ]
    children = []
    if command.time_specified_flag:
        children.append(
            f'<scte35:Program><scte35:SpliceTime ptsTime="{_xml_ticks(command.pts_time)}"/></scte35:Program>'
        )
    if command.duration_flag:
        children.append(
            f'<scte35:BreakDuration autoReturn="{_xml_bool(command.break_auto_return)}" '
            f'duration="{_xml_ticks(command.break_duration)}"/>'
        )
    if not children:
        return f"<scte35:SpliceInsert{_xml_attrs(attrs)}/>"
    return f"<scte35:SpliceInsert{_xml_attrs(attrs)}>" + "".join(children) + "</scte35:SpliceInsert>"


def _time_signal_xml(command) -> str:
    if not command.time_specified_flag:
        return "<scte35:TimeSignal/>"
    return f'<scte35:TimeSignal><scte35:SpliceTime ptsTime="{_xml_ticks(command.pts_time)}"/></scte35:TimeSignal>'


def _avail_descriptor_xml(descriptor) -> str:
    return f'<scte35:AvailDescriptor providerAvailId="{descriptor.provider_avail_id}"/>'


def _segmentation_upid_xml(descriptor) -> str:
    upid = descriptor.segmentation_upid
    upid_type = descriptor.segmentation_upid_type
    if isinstance(upid, dict) and "format_identifier" in upid:
        # MPU (type 0x0c): threefive decodes the format_identifier's 4
        # ASCII bytes and the trailing private_data hex string
        # separately -- the wire text content is their concatenation,
        # `formatIdentifier`/`privateData` are each's own decimal value.
        format_id_bytes = upid["format_identifier"].encode("ascii")
        private_data_hex = upid["private_data"][2:]  # strip "0x"
        format_id = int.from_bytes(format_id_bytes, "big")
        private_data = int(private_data_hex, 16)
        text = format_id_bytes.hex() + private_data_hex
        attrs = _xml_attrs([
            ("segmentationUpidType", upid_type),
            ("segmentationUpidFormat", "hexbinary"),
            ("formatIdentifier", format_id),
            ("privateData", private_data),
        ])
        return f"<scte35:SegmentationUpid{attrs}>{text}</scte35:SegmentationUpid>"
    if isinstance(upid, str):
        attrs = _xml_attrs([
            ("segmentationUpidType", upid_type),
            ("segmentationUpidFormat", "text"),
        ])
        return f"<scte35:SegmentationUpid{attrs}>{escape(upid)}</scte35:SegmentationUpid>"
    raise RuntimeError(
        f"dash_signal_format='xml': unsupported segmentation_upid shape for "
        f"segmentation_upid_type={upid_type}: {upid!r} -- loop-dee-loop's XML "
        f"renderer only covers the UPID shapes this repo actually produces "
        f"(text/ADI and MPU)."
    )


def _segmentation_descriptor_xml(descriptor) -> str:
    attrs = [
        ("segmentationEventId", _hex_field(descriptor.segmentation_event_id)),
        ("segmentationEventCancelIndicator", _xml_bool(descriptor.segmentation_event_cancel_indicator)),
    ]
    if descriptor.segmentation_event_cancel_indicator:
        return f"<scte35:SegmentationDescriptor{_xml_attrs(attrs)}/>"
    attrs += [
        ("segmentationEventIdComplianceIndicator", _xml_bool(descriptor.segmentation_event_id_compliance_indicator)),
        ("segmentationTypeId", descriptor.segmentation_type_id),
        ("segmentNum", descriptor.segment_num),
        ("segmentsExpected", descriptor.segments_expected),
    ]
    if descriptor.sub_segment_num is not None:
        attrs += [
            ("subSegmentNum", descriptor.sub_segment_num),
            ("subSegmentsExpected", descriptor.sub_segments_expected),
        ]
    if descriptor.segmentation_duration_flag:
        attrs.append(("segmentationDuration", _xml_ticks(descriptor.segmentation_duration)))

    children = []
    if not descriptor.delivery_not_restricted_flag:
        device_restrictions = _DEVICE_RESTRICTIONS_CODES[descriptor.device_restrictions]
        children.append(
            f'<scte35:DeliveryRestrictions webDeliveryAllowedFlag="{_xml_bool(descriptor.web_delivery_allowed_flag)}" '
            f'noRegionalBlackoutFlag="{_xml_bool(descriptor.no_regional_blackout_flag)}" '
            f'archiveAllowedFlag="{_xml_bool(descriptor.archive_allowed_flag)}" '
            f'deviceRestrictions="{device_restrictions}"/>'
        )
    children.append(_segmentation_upid_xml(descriptor))
    return f"<scte35:SegmentationDescriptor{_xml_attrs(attrs)}>" + "".join(children) + "</scte35:SegmentationDescriptor>"


def build_scte35_full_xml(splice_command_b64: str) -> str:
    """Decode `splice_command_b64` and render it as a full
    <scte35:SpliceInfoSection> element per the SCTE-35 XML binding, for
    `dash_signal_format = "xml"`. Covers whatever this marker's payload
    actually carries -- a bare splice_insert (+ AvailDescriptor), or a
    time_signal + segmentation_descriptor(s) -- since loop-dee-loop only
    ever produces those two shapes (see this module's docstring).

    Every element uses the `scte35:` prefix rather than redeclaring
    `xmlns="..."` on each one -- the caller (serve.py's build_dash_manifest)
    declares `xmlns:scte35` exactly once, on the MPD root.

    Deliberately hand-rolls the XML from threefive's *decode*-side
    attributes (`Cue.decode()`, then plain attribute access) rather than
    calling threefive's own `Cue.xml()` -- that serializer's behavior was
    found to vary/break release-to-release (mis-cased attributes,
    silently dropped fields, a duplicate xmlns attribute, depending on
    the exact threefive version resolved), where the decode-side
    attributes this function reads are the same simple, stable API
    `reencode_event_ids` above already depends on.
    """
    try:
        import threefive  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "threefive is required for dash_signal_format='xml' (pip install threefive)"
        ) from exc
    cue = threefive.Cue(splice_command_b64)
    cue.decode()

    if cue.command.command_type == 5:  # Splice Insert
        command_xml = _splice_insert_xml(cue.command)
    elif cue.command.command_type == 6:  # Time Signal
        command_xml = _time_signal_xml(cue.command)
    else:
        raise RuntimeError(
            f"dash_signal_format='xml': unsupported splice_command_type "
            f"{cue.command.command_type} ({cue.command.name!r}) -- "
            f"loop-dee-loop's XML renderer only covers splice_insert and "
            f"time_signal, the two shapes this repo actually produces."
        )

    descriptors_xml = "".join(
        _avail_descriptor_xml(d) if d.tag == 0 else _segmentation_descriptor_xml(d)
        for d in cue.descriptors
    )

    info = cue.info_section
    sis_attrs = _xml_attrs([
        ("ptsAdjustment", _xml_ticks(info.pts_adjustment)),
        ("protocolVersion", info.protocol_version),
        ("sapType", _hex_field(info.sap_type)),
        ("tier", _hex_field(info.tier)),
    ])
    return (
        f"<scte35:SpliceInfoSection{sis_attrs}>"
        f"{command_xml}{descriptors_xml}</scte35:SpliceInfoSection>"
    )


def _segmentation_type_id(marker: SignalingMarker) -> int:
    # A bare splice_insert has no segmentation_descriptor; its SCTE-35
    # splice_command_type is 0x05, represented by the agreed SPI fallback.
    return int(marker.segmentation_type_id, 16) if marker.segmentation_type_id is not None else 0x05


def _segmentation_code(marker: SignalingMarker) -> str:
    if marker.segmentation_type_id is None:
        return "SPI" + ("s" if marker.is_out else "e")
    type_id = _segmentation_type_id(marker)
    # Prefer an explicit code for this exact type (when the compact-code
    # table has one). End IDs without their own code inherit their paired
    # start's code, with the suffix retaining direction.
    base = SEGMENTATION_TYPE_CODES.get(type_id)
    if base is None and type_id in SEGMENTATION_START_TYPE_FOR_END:
        base = SEGMENTATION_TYPE_CODES.get(SEGMENTATION_START_TYPE_FOR_END[type_id])
    if base is None:
        base = f"0x{type_id:02X}"
    return base + ("s" if marker.is_out else "e")


def _segmentation_name(marker: SignalingMarker) -> str:
    if marker.segmentation_type_id is None:
        name = "splice-insert"
    else:
        type_id = _segmentation_type_id(marker)
        name = SEGMENTATION_TYPE_NAMES.get(type_id)
        if name is None and type_id in SEGMENTATION_START_TYPE_FOR_END:
            name = SEGMENTATION_TYPE_NAMES.get(SEGMENTATION_START_TYPE_FOR_END[type_id])
        if name is None:
            name = f"0x{type_id:02x}"
    suffix = "start" if marker.is_out else "end"
    return f"{name.lower().replace(' ', '-')}-{suffix}"


def _render_daterange_id(
    template: str,
    marker: SignalingMarker,
    loop_number: int,
    start_date: _dt.datetime,
) -> str:
    type_id = _segmentation_type_id(marker)
    event_id = int(marker.event_id, 16)
    if start_date.tzinfo is None:
        start_date = start_date.replace(tzinfo=_dt.timezone.utc)
    pd = start_date.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    epoch_delta = start_date - _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
    epoch = (
        epoch_delta.days * 86_400_000
        + epoch_delta.seconds * 1_000
        + epoch_delta.microseconds // 1_000
    )
    values = {
        "loop": loop_number,
        "eventid": event_id,
        "segid": type_id,
        "seghex": f"0x{type_id:02X}",
        "segcode": _segmentation_code(marker),
        "segname": _segmentation_name(marker),
        "epoch": epoch,
        "pd": pd,
    }
    try:
        rendered = template.format_map(values)
    except (KeyError, ValueError, IndexError) as exc:
        raise ValueError(f"invalid daterange_id_format: {exc}") from exc
    # RFC 8216 quoted-string forbids double quotes, CR, and LF. Playlist
    # text also forbids other control characters; strip them so the output
    # remains a syntactically valid HLS attribute value.
    rendered = "".join(
        char for char in rendered
        if char != '"' and char not in "\r\n" and unicodedata.category(char) != "Cc"
    )
    return unicodedata.normalize("NFC", rendered)


def _legacy_daterange_id(marker: SignalingMarker, loop_number: int) -> str:
    event_id = int(marker.event_id, 16)
    if marker.segmentation_type_id is not None:
        return f"{int(marker.segmentation_type_id, 16)}-{event_id}-{loop_number}"
    direction = "splice-out" if marker.is_out else "splice-in"
    return f"{direction}-{event_id}-{loop_number}"


def _sanitize_daterange_id(value: str) -> str:
    # RFC 8216 quoted-string excludes double quote, CR, and LF; playlist
    # text excludes control characters. Normalize ID text to NFC as well.
    value = "".join(
        char for char in value
        if char != '"' and char not in "\r\n" and unicodedata.category(char) != "Cc"
    )
    return unicodedata.normalize("NFC", value)


def is_instant_segmentation(m: dict) -> bool:
    """Whether a raw `.markers.json` entry is a standalone/instant signal
    (see `INSTANT_SEGMENTATION_TYPE_IDS`) rather than one half of a
    Start/End pair. franken-ts only ever emits ONE marker entry for these
    (no matching stop event with `segmentation_type_id + 1`), so they must
    never be treated as an open-ended CUE-OUT interval that some later
    CUE-IN will close -- there is no such CUE-IN coming, on this loop or
    any other.
    """
    if "is_instant" in m:
        return bool(m["is_instant"])
    seg_type_id = m.get("segmentation_type_id")
    if seg_type_id is None:
        return False
    type_id_int = int(seg_type_id, 16) if isinstance(seg_type_id, str) else seg_type_id
    return f"0x{type_id_int:02X}" in INSTANT_SEGMENTATION_TYPE_IDS


def is_out_type_id(type_id: int) -> bool:
    if type_id in SEGMENTATION_END_TYPE_IDS:
        return type_id in SEGMENTATION_START_TYPE_IDS
    return True


def is_standalone_instant_type_id(type_id: int) -> bool:
    return f"0x{type_id:02X}" in INSTANT_SEGMENTATION_TYPE_IDS


def is_out_marker(m: dict) -> bool:
    """Whether a raw `.markers.json` entry is a CUE-OUT (ad-break start) as
    opposed to a CUE-IN (ad-break end). Shared between `markers_to_signaling`
    (HLS/DASH tag authoring) and serve.py's marker/segment-overlap placement
    logic, so both agree on which markers carry a real, forward-looking
    active interval (see serve.py's `_marker_covers_segment`) versus which
    are a single point-in-time signal.

    New sidecars carry an explicit `is_out`; older sidecars use the Table 23
    End ID set rather than assuming every even/odd ID pair is consecutive.

    NOT meaningful for standalone/instant signals (see
    `is_instant_segmentation`) -- callers must check that first: an instant
    marker's even/odd type_id happens to satisfy this function's rule too
    (e.g. 0x02 is even), but it is never actually a CUE-OUT half of a pair.
    """
    splice_type = m.get("splice_type")
    seg_type_id = m.get("segmentation_type_id")
    if "is_out" in m:
        return bool(m["is_out"])
    if seg_type_id is not None:
        type_id_int = int(seg_type_id, 16) if isinstance(seg_type_id, str) else seg_type_id
        return is_out_type_id(type_id_int)
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
        is_instant = is_instant_segmentation(m)

        result.append(
            SignalingMarker(
                event_id=m["event_id"],
                pts_time_ticks=m["pts_time_ticks"],
                segmentation_type_id=seg_type_id,
                segmentation_duration_ticks=m.get("segmentation_duration_ticks"),
                splice_command_b64=_splice_command_base64_from_marker(m),
                is_out=is_out,
                is_instant=is_instant,
                marker_identity=(str(m["marker_identity"]) if m.get("marker_identity") is not None else None),
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
    daterange_id_format: str | None = DATERANGE_ID_FORMAT_DEFAULT,
) -> list[str]:
    """Return a list of `#EXT-X-DATERANGE:...` tag lines, one per marker.

    `program_start_datetime` is the wall-clock time corresponding to
    loop-relative tick 0 for *this* manifest response (i.e. already shifted
    by `loop_number * total_loop_duration_ticks` -- see serve.py). This
    function performs the tick->ISO8601 conversion exactly once per marker,
    as a final display step (SCOPE.md §4.2 "Hard rule").

    Follows the standard SCTE-35-in-HLS mapping: CUE-OUT markers get
    SCTE35-OUT + PLANNED-DURATION, CUE-IN markers get SCTE35-IN. The one
    exception is standalone/instant markers (see `is_instant_segmentation`,
    e.g. 0x02 Call Ad Server) -- these carry no real avail interval and no
    future CUE-IN will ever close them, so they get the generic SCTE35-CMD
    attribute instead, with no PLANNED-DURATION.

    `loop_number` is folded into the emitted DATERANGE `ID`. For a
    `time_signal` marker (real `segmentation_type_id`) the format is
    `<segmentation_type_id>-<event_id>-<loop_number>` (all decimal, e.g.
    segmentation_type_id 0x22 + event_id 0x64 + loop 3 -> "34-100-3") --
    `segmentation_type_id` comes first so tags naturally group/sort by
    signal kind (break/ppo/ad/... per SCTE-35 Table 22) before event
    identity. A bare `splice_insert` marker carries no segmentation_type_id
    to lean on, so it instead gets `splice-out-<event_id>-<loop_number>` /
    `splice-in-<event_id>-<loop_number>` -- unlike time_signal's even/odd
    Start/End type_id pair, splice_insert has nothing else to make the two
    tags' IDs differ, and an explicit (non-auto_return) pair landing in the
    same loop iteration otherwise collides on identical
    `event_id`+`loop_number` too (see the ID-collision note below). Per
    RFC 8216 §4.4.5.1, an `ID` that
    reappears across playlist reloads must carry byte-for-byte identical
    attributes every time -- but a looping channel legitimately re-signals
    the *same* underlying `event_id` every iteration with a new START-DATE
    (real wall-clock time), which would otherwise violate that rule.
    hls.js/most compliant clients detect the mismatch and silently drop the
    tag (observed: "DATERANGE tag attribute: START-DATE does not match for
    tags with ID..."), so downstream ad-signaling silently stops working
    after the first loop. Scoping the ID to the loop number gives each
    occurrence a genuinely unique, internally-consistent ID.
    """
    validate_daterange_id_format(daterange_id_format)
    tags: list[str] = []
    for marker in markers:
        offset_seconds = _iso8601_duration_seconds(marker.pts_time_ticks, timescale)
        start_date = program_start_datetime + _dt.timedelta(seconds=offset_seconds)
        start_date_str = start_date.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        if daterange_id_format is None:
            marker_id = _legacy_daterange_id(marker, loop_number)
        else:
            marker_id = _render_daterange_id(
                daterange_id_format, marker, loop_number, start_date,
            )
        if marker.marker_identity is not None:
            marker_id += f"-{marker.marker_identity}"
        marker_id = _sanitize_daterange_id(marker_id)
        attrs = [
            f'ID="{marker_id}"',
            f'START-DATE="{start_date_str}"',
            'CLASS="com.scte35"',
        ]

        if marker.is_instant:
            # Standalone signal (e.g. Call Ad Server) -- there is no
            # matching CUE-IN closing it, ever, so it must not be tagged as
            # an open CUE-OUT avail (PLANNED-DURATION + SCTE35-OUT), which
            # would leave every player thinking an ad break started and
            # never ended. Use the generic SCTE35-CMD attribute instead,
            # with no PLANNED-DURATION (there is no avail interval).
            attrs.append(f'SCTE35-CMD=0x{_b64_to_hex(marker.splice_command_b64)}')
        else:
            if marker.segmentation_duration_ticks is not None:
                duration_seconds = _iso8601_duration_seconds(
                    marker.segmentation_duration_ticks, timescale
                )
                attrs.append(f'PLANNED-DURATION={duration_seconds:.3f}')

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
        event_attrs["id"] = (
            f"{marker.event_id}-{marker.marker_identity}"
            if marker.marker_identity is not None else marker.event_id
        )

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


# ── [markers].daterange_mode = "grouped" ─────────────────────────────────


def build_grouped_daterange_tags(
    markers: list[SignalingMarker],
    timescale: int,
    program_start_datetime: _dt.datetime,
    loop_number: int = 0,
    daterange_id_format: str | None = DATERANGE_ID_FORMAT_DEFAULT,
) -> list[str]:
    """Like `build_daterange_tags`, but collapses every group of markers
    sharing the same `pts_time_ticks` (e.g. a Break start + nested PPO
    start + nested Ad start -- possibly alongside an unrelated instant
    signal like 0x02 Call Ad Server, all coincident in one physical
    message -- see PlaybackPanel.vue's "OUT+IN+CMD all merged into one
    wire message" comment) into a single tag instead of one tag per
    descriptor -- see `[markers].daterange_mode = "grouped"`.

    Requires the group's markers to share one `splice_command_b64` (true
    by construction for coincident descriptors sharing one physical
    SCTE-35 message -- see bake.py's `decode_embedded_scte35` module
    docstring): the shared payload is embedded once per group, under
    `ID="group-<min event_id decimal>-<loop_number>"`, as the generic
    `SCTE35-CMD` attribute -- never `SCTE35-OUT`/`SCTE35-IN` +
    `PLANNED-DURATION`, since a real coincident group can (and, per
    PlaybackPanel.vue's comment above, does in practice) mix directions
    and durations across its members with no single one of them
    correctly describing the group as a whole. A player decodes the
    payload itself regardless of which attribute delivered it (see
    PlaybackPanel.vue's `describeAllMarkers()`), so nothing is lost.

    A group of size 1 (instant or not) falls back to
    `build_daterange_tags`'s exact per-marker ID scheme and its normal
    OUT/IN/CMD attribute choice, for continuity with ungrouped output.
    """
    validate_daterange_id_format(daterange_id_format)
    groups: dict[int, list[SignalingMarker]] = {}
    order: list[int] = []
    for marker in markers:
        if marker.pts_time_ticks not in groups:
            order.append(marker.pts_time_ticks)
        groups.setdefault(marker.pts_time_ticks, []).append(marker)

    tags: list[str] = []
    for pts in order:
        group = groups[pts]
        if len(group) == 1:
            tags.extend(
                build_daterange_tags(
                    group, timescale, program_start_datetime, loop_number,
                    daterange_id_format,
                )
            )
            continue

        offset_seconds = _iso8601_duration_seconds(pts, timescale)
        start_date = program_start_datetime + _dt.timedelta(seconds=offset_seconds)
        start_date_str = start_date.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        representative = next(
            (m for m in group if m.is_out and not m.is_instant),
            next((m for m in group if not m.is_out and not m.is_instant), group[0]),
        )
        payload_hex = _b64_to_hex(group[0].splice_command_b64)
        attrs = [
            f'ID="{_render_daterange_id(daterange_id_format, representative, loop_number, start_date)}"',
            f'START-DATE="{start_date_str}"',
            'CLASS="com.scte35"',
            f'SCTE35-CMD=0x{payload_hex}',
        ]

        tags.append("#EXT-X-DATERANGE:" + ",".join(attrs))

    return tags


# ── [markers].cue_tags = "alongside" | "only" ────────────────────────────


def group_markers_by_event_id(markers: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for m in markers:
        grouped.setdefault(m["event_id"], []).append(m)
    return grouped


def group_markers_by_identity(markers: list[dict]) -> dict[tuple[str, str | None], list[dict]]:
    grouped: dict[tuple[str, str | None], list[dict]] = {}
    for marker in markers:
        grouped.setdefault((marker["event_id"], marker.get("marker_identity")), []).append(marker)
    return grouped


def resolve_marker_duration_ticks(
    marker: dict, markers_by_event_id: dict
) -> int | None:
    """Best-effort duration (in ticks) for a CUE-OUT-style DURATION
    attribute. `segmentation_duration_ticks` is present for every
    `time_signal` marker (franken_ts/markers.py always sets it there) but
    is absent for bare `splice_insert` markers -- for those, fall back to
    the paired stop marker's own `pts_time_ticks` (same `event_id`,
    `is_out` False) minus this marker's, since franken-ts always emits an
    explicit start+stop pair sharing one event_id. Returns None if `marker`
    isn't an OUT marker, or has no paired stop marker to derive a fallback
    from."""
    if marker.get("segmentation_duration_ticks") is not None:
        return marker["segmentation_duration_ticks"]
    if not is_out_marker(marker):
        return None
    stop = next(
        (
            m
            for m in markers_by_event_id.get(
                (marker["event_id"], marker.get("marker_identity")),
                markers_by_event_id.get(marker["event_id"], []),
            )
            if m is not marker and not is_out_marker(m)
        ),
        None,
    )
    if stop is None:
        return None
    return stop["pts_time_ticks"] - marker["pts_time_ticks"]


def build_cue_breaks(markers: list[dict]) -> list[dict]:
    """Every bare `splice_insert` OUT/IN pair's `[start_ticks, end_ticks)`
    interval, for `#EXT-X-CUE-OUT`/`-CONT`/`-IN` placement -- see
    `[markers].cue_tags = "alongside" | "only"`.

    Deliberately `splice_insert`-ONLY, in both `cue_tags` modes (not just
    `"only"`, where bake.py already hard-validates it): a real
    `time_signal` source can have several coincident-but-differently-
    *durationed* segmentation types active at once (e.g. a 30s Break
    containing a 5s PPO containing a 10s Ad) -- each would otherwise
    become its own independent CUE-OUT/-CONT/-IN sequence, so a single
    moment ends up under two or three simultaneously-open "avails" with
    different DURATIONs. CUE-OUT/-IN has no way to represent that
    nesting (unlike DATERANGE, where each descriptor gets its own
    independent tag) -- `splice_insert` markers are always flat,
    non-overlapping avails by construction, which is what this tag pair
    actually models.
    """
    splice_insert_markers = [m for m in markers if m.get("splice_type") == "splice_insert"]
    markers_by_event_id = group_markers_by_identity(splice_insert_markers)
    breaks: list[dict] = []
    for m in splice_insert_markers:
        if not is_out_marker(m):
            continue
        duration_ticks = resolve_marker_duration_ticks(m, markers_by_event_id)
        if duration_ticks is None:
            continue
        breaks.append({
            "event_id": m["event_id"],
            "start_ticks": m["pts_time_ticks"],
            "end_ticks": m["pts_time_ticks"] + duration_ticks,
            "duration_ticks": duration_ticks,
        })
    return breaks


def build_cue_out_tag(duration_ticks: int, timescale: int) -> str:
    duration_seconds = _iso8601_duration_seconds(duration_ticks, timescale)
    return f"#EXT-X-CUE-OUT:DURATION={duration_seconds:.3f}"


def build_cue_out_cont_tag(elapsed_ticks: int, duration_ticks: int, timescale: int) -> str:
    elapsed_seconds = _iso8601_duration_seconds(elapsed_ticks, timescale)
    duration_seconds = _iso8601_duration_seconds(duration_ticks, timescale)
    return f"#EXT-X-CUE-OUT-CONT:ELAPSED-TIME={elapsed_seconds:.3f},DURATION={duration_seconds:.3f}"


def build_cue_in_tag() -> str:
    return "#EXT-X-CUE-IN"


# ── [markers].increment_event_ids ────────────────────────────────────────

SCTE35_EVENT_ID_MAX = 0xFFFFFFFF


def compute_event_id_step(base_event_ids: list[str]) -> int:
    """The smallest power of 10 strictly greater than the largest base
    event id across the whole channel (e.g. base ids 100-190 -> step
    1000) -- the per-loop increment used by `compute_incremented_event_id`.

    A single shared step (not one derived per-marker) keeps every
    marker's own base id recognizable as the low-order remainder of its
    incremented id on every loop, and keeps the loops themselves
    identifiable from the high-order part: `event_id % step` recovers
    the original per-marker id, `event_id // step` recovers the loop
    number an id was emitted on -- both computable by inspection, from
    the id alone, without needing this channel's bake-time marker list
    or any other side channel. Returns 10 if `base_event_ids` is empty
    (no markers to derive a range from)."""
    if not base_event_ids:
        return 10
    max_id = max(int(eid, 16) for eid in base_event_ids)
    step = 10
    while step <= max_id:
        step *= 10
    return step


def compute_incremented_event_id(base_event_id_hex: str, loop_number: int, step: int) -> str:
    """SCTE-35 `splice_event_id`/`segmentation_event_id` are 32-bit fields.
    Returns `base + loop_number * step` (see `compute_event_id_step` for
    `step`), formatted the same `0x%08X` way markers.json uses --
    wrapping the loop-number component back to 0 (i.e. the id back to
    `base_event_id_hex` itself) once `loop_number * step` would push the
    id past the 32-bit ceiling, so a long-running channel never emits an
    unexpectedly small/reused-looking id after wraparound.

    `loop_number` itself is always derived from wall-clock time against
    the channel's epoch (see serve.py's `compute_loop_position`), so the
    id emitted at any given moment is fully predictable in advance from
    nothing but the channel's epoch, `step`, and each marker's base id --
    no runtime counter state involved.
    """
    base = int(base_event_id_hex, 16)
    if loop_number <= 0:
        return base_event_id_hex
    max_loop_number = (SCTE35_EVENT_ID_MAX - base) // step
    wrapped_loop_number = loop_number % (max_loop_number + 1)
    incremented = base + wrapped_loop_number * step
    return f"0x{incremented:08X}"


def build_event_id_map(markers: list[dict], loop_number: int) -> dict[str, str]:
    """`{event_id: incremented_event_id}` for every marker, computed once
    per loop iteration -- see `compute_incremented_event_id` and
    `compute_event_id_step` (the step is derived from every marker's own
    base id here, not just the ones in a given request's window, so it
    stays identical across every request/period for this channel)."""
    step = compute_event_id_step([m["event_id"] for m in markers])
    return {
        m["event_id"]: compute_incremented_event_id(m["event_id"], loop_number, step)
        for m in markers
    }


def reencode_event_ids(splice_command_b64: str, id_map: dict[str, str]) -> str:
    """Re-encode `splice_command_b64` with every `splice_event_id`/
    `segmentation_event_id` present as a key in `id_map` remapped to its
    mapped value, recomputing length/CRC via `threefive` -- the same
    decode-mutate-`encode()` pattern bake.py's `narrow_descriptors` path
    already uses (see its module docstring), just remapping ids instead of
    dropping descriptors. No-op (returns `splice_command_b64` unchanged)
    if nothing in the message matches `id_map` (e.g. `loop_number == 0`,
    where every mapped value equals its own key)."""
    try:
        import threefive  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "threefive is required for event id re-encoding (pip install threefive)"
        ) from exc

    cue = threefive.Cue(splice_command_b64)
    cue.decode()
    changed = False

    # `cue.command.splice_event_id` (splice_insert) and
    # `descriptor.segmentation_event_id` (segmentation descriptors)
    # decode to DIFFERENT Python types in threefive -- an int and a hex
    # string ('0x64', not zero-padded), respectively -- and threefive's
    # own encoder is equally particular about what it accepts back:
    # assigning an int to `segmentation_event_id` silently corrupts the
    # descriptor's encoded length (threefive logs "should be type str"
    # and .encode() then raises deep inside a later decode). Each field
    # must be read and written back in its own native type.
    cmd_event_id = getattr(cue.command, "splice_event_id", None)
    if cmd_event_id is not None:
        key = f"0x{cmd_event_id:08X}"
        new_value = id_map.get(key)
        if new_value is not None and new_value != key:
            cue.command.splice_event_id = int(new_value, 16)
            changed = True

    for descriptor in cue.descriptors:
        seg_event_id = getattr(descriptor, "segmentation_event_id", None)
        if seg_event_id is None:
            continue
        seg_event_id_int = int(seg_event_id, 16) if isinstance(seg_event_id, str) else seg_event_id
        key = f"0x{seg_event_id_int:08X}"
        new_value = id_map.get(key)
        if new_value is not None and new_value != key:
            descriptor.segmentation_event_id = new_value
            changed = True

    if not changed:
        return splice_command_b64
    return cue.encode()
