"""DASH manifest parsing (SCOPE.md §5.1) via `mpd-inspector`.

`SegmentTimeline`/`SegmentTemplate` -> `TimingSegment`s directly in ticks,
no wall-clock needed (unlike HLS). A new `<Period>` -> `asset_boundary=True`
on its first segment. Markers come from `<EventStream>`:
`presentationTime`+`duration` are already Period-relative ticks --
`mpd-inspector`'s own `EventInspector.relative_presentation_time`/
`.duration` compute exactly that for us.
"""

from __future__ import annotations

from mpd_inspector import MPDInspector, MPDParser
from mpd_inspector.inspector import (
    AdaptationSetInspector,
    PeriodInspector,
    Scte35BinaryEventInspector,
    Scte35XmlEventInspector,
)

from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from .models import AssetBoundary, RawMarker, TimingSegment

TIMESCALE = 90_000


def _select_reference_adaptation_set(period: PeriodInspector) -> AdaptationSetInspector:
    """Prefer a video adaptation set for segment timing (matches HLS's
    implicit video-driven segmentation); fall back to the first available
    adaptation set (e.g. an audio-only period) if there is no video."""
    adaptation_sets = period.adaptation_sets
    if not adaptation_sets:
        raise ValueError(f"Period {period.index} has no AdaptationSets")
    for adaptation_set in adaptation_sets:
        mime_type = adaptation_set.mime_type or ""
        if mime_type.startswith("video/"):
            return adaptation_set
    return adaptation_sets[0]


def _dash_init_uri(root, period_idx: int, representation, manifest_url: str) -> str | None:
    """Best-effort absolute URL of the reference Representation's init
    segment: the `SegmentTemplate@initialization` nearest the Representation
    (Representation, then its AdaptationSet, then the Period), with
    $RepresentationID$/$Bandwidth$ substituted. mpd-inspector doesn't expose
    it. None when the MPD declares no template init (e.g. self-initializing
    segments, or SegmentBase/SegmentList addressing)."""
    ns = {"m": "urn:mpeg:dash:schema:mpd:2011"}
    periods = root.findall("m:Period", ns)
    if period_idx >= len(periods):
        return None
    rep_id = str(getattr(representation, "id", "") or "")
    for adaptation in periods[period_idx].findall("m:AdaptationSet", ns):
        for rep in adaptation.findall("m:Representation", ns):
            if rep_id and rep.get("id") != rep_id:
                continue
            for scope in (rep, adaptation, periods[period_idx]):
                template = scope.find("m:SegmentTemplate", ns)
                if template is not None and template.get("initialization"):
                    init = (
                        template.get("initialization")
                        .replace("$RepresentationID$", rep.get("id", ""))
                        .replace("$Bandwidth$", rep.get("bandwidth", ""))
                    )
                    return urljoin(manifest_url, init)
            return None
    return None


def _reference_representations(period: PeriodInspector) -> list:
    return list(_select_reference_adaptation_set(period).representations)


def list_dash_representations(manifest_text: str, manifest_url: str = "") -> list[dict]:
    """The video ladder of an MPD: one dict per Representation of the first
    Period's reference (video) AdaptationSet -- `id`, `bandwidth`, `resolution`,
    `codecs`, `frame_rate` (see `dash_is_static` for the VOD check)."""
    inspector = MPDInspector(MPDParser.from_string(manifest_text))
    root = ET.fromstring(manifest_text.encode("utf-8"))
    ns = {"m": "urn:mpeg:dash:schema:mpd:2011"}
    if not inspector.periods:
        return []
    out = []
    adaptation_set = _select_reference_adaptation_set(inspector.periods[0])
    rep_elements = {
        rep.get("id"): (adaptation, rep)
        for adaptation in root.findall("m:Period", ns)[0].findall("m:AdaptationSet", ns)
        for rep in adaptation.findall("m:Representation", ns)
    }
    for representation in adaptation_set.representations:
        adaptation_element, rep_element = rep_elements.get(str(representation.id), (None, None))
        def _attr(name):
            for element in (rep_element, adaptation_element):
                if element is not None and element.get(name):
                    return element.get(name)
            return None
        frame_rate = _attr("frameRate")
        out.append({
            "id": str(representation.id),
            "bandwidth": int(_attr("bandwidth")) if _attr("bandwidth") else None,
            "resolution": f"{representation.width}x{representation.height}" if representation.width and representation.height else None,
            "codecs": _attr("codecs"),
            "frame_rate": _parse_frame_rate(frame_rate),
        })
    return out


def _parse_frame_rate(value: str | None) -> float | None:
    if not value:
        return None
    numerator, _, denominator = value.partition("/")
    return float(numerator) / float(denominator or 1)


def dash_is_static(manifest_text: str) -> bool:
    return ET.fromstring(manifest_text.encode("utf-8")).get("type", "static") == "static"


def extract_dash(
    manifest_text: str, manifest_url: str = "", representation_id: str | None = None
) -> tuple[list[TimingSegment], list[RawMarker], list[AssetBoundary]]:
    """Parse one DASH MPD snapshot into normalized TimingSegment/RawMarker/
    AssetBoundary lists, positioned on THIS snapshot's own timeline
    (starting at tick 0, concatenating every Period in document order) --
    the caller combines multiple snapshots over time into one overall
    timeline (SCOPE.md §7, see boundaries.py). `representation_id` picks a
    specific Representation of the video ladder (default: the first)."""
    mpd = MPDParser.from_string(manifest_text)
    inspector = MPDInspector(mpd)
    # mpd-inspector's full_urls properties use `self.base_uri` verbatim (no
    # implicit dirname-of-manifest-URL step, unlike m3u8.loads(uri=...)) --
    # must be the manifest's own DIRECTORY, not the manifest URL itself, or
    # every resolved segment URL comes out as "<manifest-url><media-path>"
    # with no separator.
    inspector.base_uri = manifest_url.rsplit("/", 1)[0] + "/" if manifest_url else ""

    manifest_root = ET.fromstring(manifest_text.encode("utf-8"))
    segments: list[TimingSegment] = []
    markers: list[RawMarker] = []
    boundaries: list[AssetBoundary] = []
    cumulative_ticks = 0
    global_index = 0

    for period_idx, period in enumerate(inspector.periods):
        adaptation_set = _select_reference_adaptation_set(period)
        if representation_id is None:
            representation = adaptation_set.representations[0]
        else:
            representation = next(
                (r for r in adaptation_set.representations if str(r.id) == representation_id), None
            )
            if representation is None:
                raise ValueError(
                    f"Period {period_idx} has no Representation with id {representation_id!r} in its "
                    f"video AdaptationSet -- a ladder must expose the same Representations in every Period."
                )
        media_segments = representation.segment_information.segments
        init_uri = _dash_init_uri(manifest_root, period_idx, representation, manifest_url)

        period_start_ticks = cumulative_ticks
        if period_idx > 0:
            boundaries.append(
                AssetBoundary(
                    segment_index=global_index,
                    gap_ticks=_dash_gap_ticks(period, period_start_ticks),
                )
            )
        for seg_idx, media_segment in enumerate(media_segments):
            duration_ticks = round(media_segment.duration * TIMESCALE)
            segments.append(
                TimingSegment(
                    index=global_index,
                    duration_ticks=duration_ticks,
                    # A new Period always starts a new asset EXCEPT the
                    # very first one -- same convention as HLS, which never
                    # tags a #EXT-X-DISCONTINUITY before the first segment
                    # either (it's implicit/redundant; serve.py's own
                    # boundary set always includes index 0 regardless, see
                    # loop-dee-loop/SCOPE.md §6.3).
                    asset_boundary=(seg_idx == 0 and period_idx > 0),
                    source_uri=(media_segment.urls[0] if media_segment.urls else None),
                    init_uri=init_uri,
                )
            )
            cumulative_ticks += duration_ticks
            global_index += 1

        for event_stream in period.event_streams:
            for event in event_stream.events:
                if not isinstance(event, (Scte35BinaryEventInspector, Scte35XmlEventInspector)):
                    continue  # not a SCTE-35 EventStream (some other schemeIdUri)
                relative_ticks = round(event.relative_presentation_time.total_seconds() * TIMESCALE)
                pts_time_ticks = period_start_ticks + relative_ticks
                declared_duration_ticks = (
                    round(event.duration.total_seconds() * TIMESCALE)
                    if event.duration is not None
                    else None
                )
                markers.append(
                    RawMarker(
                        source=(
                            "eventstream-bin"
                            if isinstance(event, Scte35BinaryEventInspector)
                            else "eventstream-xml"
                        ),
                        pts_time_ticks=pts_time_ticks,
                        # mpd-inspector exposes no public accessor for the
                        # raw lxml element -- `._tag.element` is its own
                        # internal representation (BaseInspector.__init__),
                        # handed to threefive for decode (§5.2), which
                        # accepts XML directly (both the xml+bin <Binary>
                        # wrapper and a bare <SpliceInfoSection>).
                        splice_command_xml=event._tag.element,
                        declared_duration_ticks=declared_duration_ticks,
                    )
                )

    return segments, markers, boundaries


def _dash_gap_ticks(period: PeriodInspector, period_real_start_ticks: int) -> int:
    """SCOPE.md §6.2: `Period start=` (if explicitly declared in the MPD)
    vs the previous Period's own cumulative real duration -- no wall-clock
    needed for DASH at all. `period._tag.start` is the RAW `@start`
    attribute (a timedelta, relative to the MPD/Period-sequence start)
    before mpd-inspector's own PeriodInspector.start_time folds it into an
    absolute datetime for dynamic presentations -- using the raw value
    here avoids having to undo that folding. No `@start` attribute at all
    means "start immediately after the previous Period" per the DASH
    spec's own default, i.e. no declared gap/overlap: 0."""
    declared_start = period._tag.start
    if declared_start is None:
        return 0
    declared_start_ticks = round(declared_start.total_seconds() * TIMESCALE)
    return declared_start_ticks - period_real_start_ticks
