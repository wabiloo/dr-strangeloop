"""HLS manifest parsing (SCOPE.md §5.1) via `m3u8` (globo/globocom).

Accumulates `#EXTINF` durations into `TimingSegment`s;
`#EXT-X-DISCONTINUITY` -> `asset_boundary=True`. Markers come from
`DateRange`/`SCTE35-OUT`/`-IN`/`-CMD` (HLS-native DATERANGE convention) or
per-segment `.cue_out`/`.scte35` (Comcast-style tags).

`#EXT-X-PROGRAM-DATE-TIME` positions a DATERANGE against the timeline:
`START-DATE` is absolute wall-clock, matched to the segment whose PDT
window contains it, then accumulated segment durations up to that point
give `pts_time_ticks`. **Gotcha** (SCOPE.md §5.1): `#EXT-X-DISCONTINUITY`
doesn't reliably reset PROGRAM-DATE-TIME consistently across packagers --
each discontinuity run is treated as its own independent time-base for
this matching, never assuming monotonic wall-clock across the whole
playlist.
"""

from __future__ import annotations

import datetime as _dt

import m3u8

from .models import AssetBoundary, RawMarker, TimingSegment

TIMESCALE = 90_000


def _parse_iso8601(value: str) -> _dt.datetime:
    # HLS timestamps are RFC 3339 / ISO 8601 with an explicit offset (often
    # "Z"). Python's fromisoformat doesn't accept a bare "Z" before 3.11's
    # improvements landed everywhere reliably, so normalize it.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(value)


def assign_program_date_times(segments: list) -> list[_dt.datetime | None]:
    """Forward-carry each segment's implied PROGRAM-DATE-TIME from the last
    explicit tag, resetting at every discontinuity (own independent
    time-base per run -- see module docstring's gotcha)."""
    pdts: list[_dt.datetime | None] = []
    current: _dt.datetime | None = None
    for seg in segments:
        if seg.discontinuity:
            current = None
        if seg.program_date_time is not None:
            current = seg.program_date_time
        if current is not None:
            pdts.append(current)
            current = current + _dt.timedelta(seconds=seg.duration or 0.0)
        else:
            pdts.append(None)
    return pdts


def extract_hls(
    manifest_text: str, manifest_url: str
) -> tuple[list[TimingSegment], list[RawMarker], list[AssetBoundary]]:
    """Parse one HLS media playlist snapshot into normalized
    TimingSegment/RawMarker/AssetBoundary lists, positioned on THIS
    snapshot's own timeline (starting at tick 0) -- the caller is
    responsible for combining multiple snapshots over time (a
    ManifestStream) into one overall timeline (SCOPE.md §7, see
    boundaries.py).
    """
    playlist = m3u8.loads(manifest_text, uri=manifest_url)
    pdts = assign_program_date_times(playlist.segments)

    segments: list[TimingSegment] = []
    markers: list[RawMarker] = []
    boundaries: list[AssetBoundary] = []
    cumulative_ticks = 0
    starts_ticks: list[int] = []

    for index, seg in enumerate(playlist.segments):
        duration_ticks = round((seg.duration or 0.0) * TIMESCALE)
        starts_ticks.append(cumulative_ticks)
        segments.append(
            TimingSegment(
                index=index,
                duration_ticks=duration_ticks,
                asset_boundary=bool(seg.discontinuity),
                source_uri=seg.absolute_uri or None,
            )
        )

        if seg.discontinuity:
            boundaries.append(
                AssetBoundary(segment_index=index, gap_ticks=_hls_gap_ticks(index, pdts, segments))
            )

        for daterange in seg.dateranges:
            pts_time_ticks = _daterange_pts_time_ticks(
                daterange, segment_index=index, pdts=pdts, starts_ticks=starts_ticks
            )
            if pts_time_ticks is None:
                continue
            splice_command_b64 = (
                daterange.scte35_out or daterange.scte35_in or daterange.scte35_cmd
            )
            if splice_command_b64 is None:
                continue
            declared_duration_ticks = None
            if daterange.planned_duration is not None:
                declared_duration_ticks = round(daterange.planned_duration * TIMESCALE)
            elif daterange.duration is not None:
                declared_duration_ticks = round(daterange.duration * TIMESCALE)
            markers.append(
                RawMarker(
                    source="daterange",
                    pts_time_ticks=pts_time_ticks,
                    splice_command_b64=splice_command_b64,
                    declared_duration_ticks=declared_duration_ticks,
                )
            )

        # Comcast-style #EXT-X-CUE-OUT (+ its real SCTE-35 payload, via
        # #EXT-OATCLS-SCTE35 or a CUE= attribute on the tag itself --
        # `seg.scte35`/`seg.oatcls_scte35`) only ever carries ONE genuine
        # decodable SCTE-35 message, attached at the break's START -- the
        # matching #EXT-X-CUE-IN is a playlist-level signal only, with no
        # separate SCTE-35 message of its own in this tagging style. Emit
        # exactly one RawMarker at the CUE-OUT point (never a second one
        # at CUE-IN reusing the same bytes): once decoded, its own
        # segmentation_duration_ticks describes the whole interval, the
        # same way a single duration-carrying DATERANGE/time_signal marker
        # already does elsewhere in this pipeline (see
        # serve.py's `_marker_covers_segment`, loop-dee-loop) -- a bare
        # CUE-OUT/-IN with no SCTE-35 attached carries nothing decodable
        # and is skipped entirely.
        if seg.cue_out_start:
            payload = seg.scte35 or seg.oatcls_scte35
            if payload:
                markers.append(
                    RawMarker(
                        source="cue-out",
                        pts_time_ticks=cumulative_ticks,
                        splice_command_b64=payload,
                        declared_duration_ticks=(
                            round(float(seg.scte35_duration) * TIMESCALE)
                            if getattr(seg, "scte35_duration", None)
                            else None
                        ),
                    )
                )

        cumulative_ticks += duration_ticks

    return segments, markers, boundaries


def _hls_gap_ticks(
    boundary_index: int, pdts: list[_dt.datetime | None], segments: list[TimingSegment]
) -> int:
    """SCOPE.md §6.2: the last segment's end wall-clock in asset N vs the
    first segment's start wall-clock in asset N+1, using each run's own
    local PDT (never assumed monotonic across the whole playlist -- see
    module docstring). 0 (no declared gap/overlap) when either side has no
    PDT to measure against, or this is the very first boundary (nothing
    precedes it)."""
    if boundary_index == 0:
        return 0
    prev_pdt = pdts[boundary_index - 1]
    this_pdt = pdts[boundary_index]
    if prev_pdt is None or this_pdt is None:
        return 0
    prev_end_wall_clock = prev_pdt + _dt.timedelta(
        seconds=segments[boundary_index - 1].duration_ticks / TIMESCALE
    )
    gap_seconds = (this_pdt - prev_end_wall_clock).total_seconds()
    return round(gap_seconds * TIMESCALE)


def _daterange_pts_time_ticks(
    daterange, *, segment_index: int, pdts: list[_dt.datetime | None], starts_ticks: list[int]
) -> int | None:
    """SCOPE.md §5.1: position a DATERANGE using its own absolute
    START-DATE against the segment m3u8 attached it to, refined by the
    wall-clock delta between the two (a marker declared ahead of its own
    segment's PDT is legitimately mid-segment, not exactly at its start)."""
    if daterange.start_date is None:
        return None
    marker_wall_clock = _parse_iso8601(daterange.start_date)
    segment_pdt = pdts[segment_index]
    seg_start_ticks = starts_ticks[segment_index]
    if segment_pdt is None:
        # No wall-clock reference at all for this run (no PDT tag ever
        # seen since the last discontinuity) -- fall back to the attached
        # segment's own start, the best position information available.
        return seg_start_ticks
    delta_seconds = (marker_wall_clock - segment_pdt).total_seconds()
    return seg_start_ticks + round(delta_seconds * TIMESCALE)
