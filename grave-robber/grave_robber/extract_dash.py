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


def extract_dash(
    manifest_text: str, manifest_url: str = ""
) -> tuple[list[TimingSegment], list[RawMarker], list[AssetBoundary]]:
    """Parse one DASH MPD snapshot into normalized TimingSegment/RawMarker/
    AssetBoundary lists, positioned on THIS snapshot's own timeline
    (starting at tick 0, concatenating every Period in document order) --
    the caller combines multiple snapshots over time into one overall
    timeline (SCOPE.md §7, see boundaries.py)."""
    mpd = MPDParser.from_string(manifest_text)
    inspector = MPDInspector(mpd)
    # mpd-inspector's full_urls properties use `self.base_uri` verbatim (no
    # implicit dirname-of-manifest-URL step, unlike m3u8.loads(uri=...)) --
    # must be the manifest's own DIRECTORY, not the manifest URL itself, or
    # every resolved segment URL comes out as "<manifest-url><media-path>"
    # with no separator.
    inspector.base_uri = manifest_url.rsplit("/", 1)[0] + "/" if manifest_url else ""

    segments: list[TimingSegment] = []
    markers: list[RawMarker] = []
    boundaries: list[AssetBoundary] = []
    cumulative_ticks = 0
    global_index = 0

    for period_idx, period in enumerate(inspector.periods):
        adaptation_set = _select_reference_adaptation_set(period)
        representation = adaptation_set.representations[0]
        media_segments = representation.segment_information.segments

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
