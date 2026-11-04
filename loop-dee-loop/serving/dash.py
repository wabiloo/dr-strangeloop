"""DASH MPDs (periodic and continuous timeline), including the low-latency attributes and clock sync."""

from __future__ import annotations

import bisect
import math

from loop_math import ticks_to_wall_clock_seconds
from scte35_signaling import (
    build_event_id_map,
    build_scte35_full_xml,
    is_instant_segmentation,
    is_out_marker,
    reencode_event_ids,
    SCTE35_XML_NAMESPACE,
)
from serving import constants
from timeshift import TimeWindow

from serving.dash_render import (
    _join_events,
    render_audio_adaptation_set,
    render_segment_timeline,
    render_video_representations,
)
from serving.markers import _asset_ids_starting_in_segment, _dash_event_comment, _marker_covers_segment



class DashMixin:
    """DASH MPDs (periodic and continuous timeline), including the low-latency attributes and clock sync."""

    @property
    def ll_template_attrs(self) -> str:
        """Extra `SegmentTemplate` attributes of a low-latency MPD (empty otherwise)."""
        if not self.ll_dash:
            return ""
        return f' availabilityTimeOffset="{self.ll_ato_seconds}" availabilityTimeComplete="false"'

    def _ll_live(self, window: "TimeWindow | None") -> bool:
        return self.ll_dash and window is None

    def _service_description_xml(self, window: "TimeWindow | None") -> str:
        """DASH-IF LL `ServiceDescription`: the latency the player should hold
        (target, with a band it may catch up or slow down within)."""
        if not self._ll_live(window):
            return ""
        target = round(self.ll_target_latency_seconds * 1000)
        return (
            '  <ServiceDescription id="0">\n'
            f'    <Latency target="{target}" min="{target // 2}" max="{target * 2}"/>\n'
            '    <PlaybackRate min="0.96" max="1.04"/>\n'
            "  </ServiceDescription>\n"
        )

    def _utc_timing_xml(self, window: "TimeWindow | None") -> str:
        """A dynamic MPD's players derive the live edge from their own clock.
        With an `offset` (SCOPE.md §13.7) "now" is not the client's now, so
        tell it what "now" is via a direct UTCTiming value -- the standard
        DASH mechanism -- and it plays as the stream would have then. Empty
        for plain live and for static (ended) MPDs."""
        if self._ll_live(window) and self.offset_ticks == 0:
            # Low latency needs the player's clock within tens of ms of ours.
            if self.ll_utc_timing == "http" and self.ll_utc_timing_url:
                return (
                    '  <UTCTiming schemeIdUri="urn:mpeg:dash:utc:http-xsdate:2014" '
                    f'value="{self.ll_utc_timing_url}"/>\n'
                )
            if self.ll_utc_timing == "direct":
                # Only as accurate as the MPD is fresh: served no-store (see dash_manifest).
                return (
                    '  <UTCTiming schemeIdUri="urn:mpeg:dash:utc:direct:2014" '
                    f'value="{self._iso_ticks(self.now_ticks())}"/>\n'
                )
            return ""
        if self.offset_ticks == 0 or (window is not None and window.ended):
            return ""
        return (
            '  <UTCTiming schemeIdUri="urn:mpeg:dash:utc:direct:2014" '
            f'value="{self._iso_ticks(self.now_ticks())}"/>\n'
        )

    def _mpd_open_tag(
        self,
        *,
        window: "TimeWindow | None",
        window_segments: int,
        origin_abs_ticks: int = 0,
        duration_ticks: int = 0,
    ) -> str:
        """The `<MPD ...>` start tag shared by both DASH builders. Live
        (window is None) is exactly the pre-§13 output. A time-shifted range
        (SCOPE.md §13): `availabilityStartTime` is the wall clock of the
        range origin so presentation time 0 == the snapped start; an ended
        range is a static MPD with a fixed duration; a growing one is
        dynamic with the whole range kept (no timeShiftBufferDepth)."""
        pkg = self.package
        max_seg = pkg.max_segment_duration_seconds_rounded_up
        ns = (
            f'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"\n'
            f'     xmlns:scte35="{SCTE35_XML_NAMESPACE}"\n'
            f'     profiles="urn:mpeg:dash:profile:isoff-live:2011"\n'
        )
        if window is not None and window.ended:
            duration = ticks_to_wall_clock_seconds(duration_ticks, pkg.timescale)
            return (
                ns + f'     type="static"\n'
                f'     mediaPresentationDuration="PT{duration}S"\n'
                f'     minBufferTime="PT2S">'
            )
        if window is not None:
            ast = self._iso_ticks(self.epoch_ticks + origin_abs_ticks)
            depth = ""
        else:
            ast = self._iso_ticks(self.epoch_ticks)
            # The window is `window_segments` segments of the loop's MEAN
            # duration (stable across refreshes); max_seg would inflate it.
            mean_ticks = pkg.total_loop_duration_ticks * window_segments / pkg.segments_per_loop
            depth = f'     timeShiftBufferDepth="PT{math.ceil(mean_ticks / pkg.timescale)}S"\n'
        return (
            ns + f'     type="dynamic"\n'
            f'     availabilityStartTime="{ast}"\n'
            f'     publishTime="{self._iso_ticks(self.now_ticks())}"\n'
            f'     minimumUpdatePeriod="PT{max_seg}S"\n'
            + depth
            + (
                f'     suggestedPresentationDelay="PT{self.ll_target_latency_seconds:g}S"\n'
                f'     minBufferTime="PT1S">'
                if self.ll_dash and window is None
                else f'     suggestedPresentationDelay="PT{max_seg * 2}S"\n'
                f'     minBufferTime="PT2S">'
            )
        )

    def build_dash_manifest(
        self, window_segments: int | None = None, window: TimeWindow | None = None
    ) -> str:
        """Build a DASH MPD (@type=dynamic) covering the same sliding window
        of segments as the HLS playlists, using one <AdaptationSet> per
        media type. The video AdaptationSet contains one <Representation>
        per rendition, each with its OWN <SegmentTemplate>/<SegmentTimeline>
        (DASH allows this at Representation level) since each rendition's
        segment boundary tick VALUES were snapped independently (same
        reasoning as audio -- see LoopPackage/VideoRendition docstrings).
        The <EventStream> carries the same markers as HLS's DATERANGE tags,
        authored directly from markers.json (never GPAC's own aggregation
        -- SCOPE.md §6).

        One <Period> per loop iteration covered by the current window,
        each starting at `PT{loop_number * total_loop_duration_seconds}S`
        (real wall-clock offset from availabilityStartTime). This mirrors
        the HLS side's #EXT-X-DISCONTINUITY fix for the same underlying
        reason: every physical segment file is reused byte-for-byte on
        every loop iteration (SCOPE.md §4.1 step 6), so its internal fMP4
        timestamps (tfdt/baseMediaDecodeTime) always restart from the same
        loop-relative values regardless of which real loop iteration is
        being served. A single, never-restarted Period whose
        <SegmentTimeline> `t` values grow forever across loops would assert
        an ever-increasing presentation timeline while the underlying
        media's actual decode timestamps repeatedly reset -- exactly the
        HLS "no discontinuity signaled" bug, just via DASH's own
        discontinuity mechanism (Periods) instead of HLS's
        #EXT-X-DISCONTINUITY tag. Starting a new Period at each loop
        boundary makes every Period's own <SegmentTimeline> genuinely
        period-relative (starting back at the segment's own loop-relative
        tick), consistent with what's actually inside the segment files,
        and gives players an explicit, spec-compliant boundary to reset
        timestamp-continuity expectations at -- rather than a single Period
        silently lying about being one continuous timeline forever.

        The window is the same sliding range HLS and /timeline.json use
        (`window_range`). The open Period is front-trimmed like any live
        window: dropping old segments is fine for players as long as the
        kept segments keep their number, `t` and the Period's `start`
        (tested 2026-10 with dash.js 5.2.1 and Shaka 5.2.12; both break
        when those change between refreshes).
        """
        if self.continuous:
            return self._build_dash_manifest_continuous(window_segments, window)

        window_segments = window_segments or self.window_segments
        pkg = self.package
        win = self.window_range(window, window_segments)

        # grave-robber/SCOPE.md §6.1: generalizes "one Period per loop
        # iteration" to "one Period per asset span" -- a span being the run
        # of segments between consecutive entries of pkg.boundaries (always
        # includes local index 0, the loop wrap). With no internal asset
        # boundaries (pkg.boundaries == {0}), there is exactly one span per
        # loop and every span_bounds/span_index_for_local call below reduces
        # to the original whole-loop behavior.
        # Period starts = real asset boundaries + forced signal-only breaks
        # (period_on_segmentation). A signal-only Period reuses its real
        # span's init segment and keeps that span's media timeline, so
        # `real_sorted` maps any Period back to the real span it lives in.
        real_sorted = sorted(pkg.boundaries)
        spans_per_loop = len(self.dash_period_starts)

        def _real_span_index(local_index: int) -> int:
            return bisect.bisect_right(real_sorted, local_index) - 1

        # One Period per (loop, span) run over the window's segments. Same rule
        # live and time-shifted: the open Period is front-trimmed like any
        # sliding live window, with every kept segment's number, `t` and the
        # Period's `start` unchanged. dash.js and Shaka accept that; they only
        # break when those change between refreshes.
        periods_plan = [
            (loop, span, s, list(range(g_first - loop * pkg.segments_per_loop, g_last - loop * pkg.segments_per_loop + 1)))
            for loop, span, s, _e, g_first, g_last in self.dash_periodic_groups(win.first, win.last)
        ]

        def _period_entries(
            boundary_ticks: list[int], local_indices: list[int], origin_local: int
        ) -> list[tuple[int, int, int]]:
            """(segment_start_ticks, duration_ticks, local_index), relative
            to the tick of segment `origin_local` -- the Period's own
            real-span start (or 0 for loop-relative), so identical to
            loop-relative in the common, no-internal-boundary case."""
            span_start_ticks = boundary_ticks[origin_local]
            entries = []
            for local_index in local_indices:
                segment_start_ticks = boundary_ticks[local_index]
                if local_index + 1 < len(boundary_ticks):
                    seg_end_ticks_local = boundary_ticks[local_index + 1]
                else:
                    seg_end_ticks_local = pkg.total_loop_duration_ticks
                duration_ticks = seg_end_ticks_local - segment_start_ticks
                entries.append((segment_start_ticks - span_start_ticks, duration_ticks, local_index))
            return entries

        window_origin_abs = 0
        window_duration_ticks = 0
        if window is not None:
            first_loop, _first_span, _first_start, first_locals = periods_plan[0]
            window_origin_abs = (
                first_loop * pkg.total_loop_duration_ticks
                + pkg.segment_boundary_ticks[first_locals[0]]
                + pkg.declared_offset_ticks_by_local_index[first_locals[0]]
            )
            last_loop, _last_span, _last_start, last_locals = periods_plan[-1]
            last_local = last_locals[-1]
            last_end_local = (
                pkg.segment_boundary_ticks[last_local + 1]
                if last_local + 1 < len(pkg.segment_boundary_ticks)
                else pkg.total_loop_duration_ticks
            )
            window_duration_ticks = (
                last_loop * pkg.total_loop_duration_ticks
                + last_end_local
                + pkg.declared_offset_ticks_by_local_index[last_local]
                - window_origin_abs
            )

        period_xml_parts = []
        for period_number, (loop_number, span_index, span_start_local, local_indices) in enumerate(periods_plan):
            if not local_indices:
                continue
            # Media-timeline origin: the real span this Period lives in. For
            # a signal-only Period (forced break inside a span) it is earlier
            # than span_start_local, `t` keeps counting from it, and the
            # difference goes into presentationTimeOffset below.
            origin_local = real_sorted[_real_span_index(span_start_local)]
            real_span_index = _real_span_index(span_start_local)

            def _extra_pto_attr(boundary_ticks: list[int], base_pto: int) -> str:
                total = base_pto + boundary_ticks[span_start_local] - boundary_ticks[origin_local]
                return f' presentationTimeOffset="{total}"' if total else ""

            # grave-robber/SCOPE.md §6.2: declared position (real serving
            # position + every gap_ticks crossed so far) drives the
            # Period's own start=, exactly mirroring the HLS side's PDT
            # computation -- 0 offset (i.e. today's plain
            # loop_number*total_loop_duration value) whenever there are no
            # internal asset boundaries.
            period_start_ticks_relative = (
                loop_number * pkg.total_loop_duration_ticks
                + pkg.segment_boundary_ticks[span_start_local]
                + pkg.declared_offset_ticks_by_local_index[span_start_local]
            )
            # SCOPE.md §13: rebase a time-shifted range so its first segment is
            # presentation time 0 -- the first Period starts at 0 with a
            # presentationTimeOffset covering the lead-in to its first
            # segment; later Periods start relative to the range origin.
            pto_ticks = 0
            if window is not None:
                if period_number == 0:
                    pto_ticks = window_origin_abs - period_start_ticks_relative
                    period_start_ticks_relative = 0
                else:
                    period_start_ticks_relative -= window_origin_abs
            period_start_seconds = ticks_to_wall_clock_seconds(
                period_start_ticks_relative, pkg.timescale
            )
            # Loop-local, like HLS's /seg/<local>: same URLs, same cache entries.
            first_number = local_indices[0]

            # Reference (ad-decision authority) entries for this period,
            # used for marker placement -- loop-relative ticks, since each
            # Period's own <EventStream> is independently time-based from
            # its own start.
            # Loop-relative (markers' and asset boundaries' own frame).
            reference_entries = [
                (t + pkg.segment_boundary_ticks[0], d, li)
                for t, d, li in _period_entries(pkg.segment_boundary_ticks, local_indices, 0)
            ]

            # Asset-boundary comments (from franken-ts's .timeline.json, see
            # bake.py's load_asset_boundaries): decided once from the
            # reference rendition's timeline, keyed by local_index so every
            # rendition's own <SegmentTimeline> (and audio's) places the
            # same asset's comment at the same segment index -- same
            # pattern as marker placement above.
            asset_ids_by_local_index: dict[int, list[str]] = {}
            for seg_start_local, duration_ticks, local_index in reference_entries:
                asset_ids = _asset_ids_starting_in_segment(
                    pkg.asset_boundaries, seg_start_local, seg_start_local + duration_ticks
                )
                if asset_ids:
                    asset_ids_by_local_index[local_index] = asset_ids

            # Loop-number comment, inside the Period before its first
            # segment: on the Period that starts a loop, and on the very
            # first Period of the manifest (whichever loop its first segment
            # belongs to). Not on later asset-span Periods of the same loop.
            emit_loop_comment = span_start_local == 0 or period_number == 0

            def _segment_timeline_xml(entries: list[tuple[int, int, int]]) -> str:
                items: list[tuple] = []
                if emit_loop_comment:
                    items.append(("c", f"loop {self._loop_label(loop_number)}"))
                for t, d, local_index in entries:
                    for asset_id in asset_ids_by_local_index.get(local_index, []):
                        items.append(("c", f"asset: {asset_id}"))
                    items.append(("s", t, d))
                return render_segment_timeline(items, self.dash_timeline == "compact")

            # `id` is a per-Period-unique synthetic id, never the raw
            # event_id directly -- see DIRECTION_CODE below. A player
            # decodes the Signal payload itself (via the `scte35` npm
            # package, same as HLS -- see igor's scte35Lite.ts
            # describeAllMarkers()) to get the real segmentation/splice
            # event id and type, rather than reading `id`.
            #
            # A Start/End pair SHARES one event_id by design (see the
            # module docstring on event_id reuse), and once enough of a
            # loop iteration has played out that BOTH halves fall inside the
            # same Period's window range, they're both due in the same
            # single <EventStream> -- if `id` were the raw event_id,
            # dash.js's EventController (confirmed against its own source)
            # would see the second one as a duplicate of the first (same
            # id) and silently drop it, so the End marker would never
            # fire. `DIRECTION_CODE` gives each direction (out/in/instant)
            # its own multiple of the base event id, so the two halves of
            # one break -- and the exact same marker recurring next loop --
            # always get distinct, but still fully deterministic (no
            # runtime counter), ids. Base event ids must stay well under
            # 2**30 for this to fit `id`'s xs:unsignedInt range once
            # multiplied -- true for every id this codebase's channel
            # configs actually assign.
            # [markers].increment_event_ids: one id map per Period, keyed
            # by that Period's own loop_number (same helper HLS uses, see
            # _remap_signaling_markers) -- {} when the setting is off, so
            # the lookups below become no-ops via dict.get() fallback.
            event_id_map = build_event_id_map(pkg.markers, loop_number) if pkg.increment_event_ids else {}
            DIRECTION_CODE = {"out": 0, "in": 1, "instant": 2}

            # [markers].dash_signal_format: "binary" (default) carries the
            # raw base64 splice command in a <scte35:Binary> element; "xml"
            # carries each marker's full decoded <scte35:SpliceInfoSection>
            # instead (see build_scte35_full_xml). schemeIdUri distinguishes
            # the two per the SCTE-35 XML binding, constant for the whole
            # manifest.
            scheme_id_uri = (
                SCTE35_XML_NAMESPACE
                if pkg.dash_signal_format == "xml"
                else "urn:scte:scte35:2014:xml+bin"
            )

            event_xml: list[tuple[int, str]] = []
            for marker in pkg.markers:
                # A single <Event> element describes the whole
                # [presentationTime, presentationTime+duration) interval on
                # its own -- unlike HLS's per-segment EXT-X-DATERANGE tags,
                # it doesn't need repeating once per overlapping segment.
                # But it must still be included in this Period as long as
                # ANY of the Period's currently-served segments overlaps
                # it (see _marker_covers_segment) -- not just the one
                # segment containing the marker's own start tick -- so a
                # player whose manifest poll lands after that first segment
                # has scrolled out of the window (while later segments of
                # the same break are still being served) still sees it.
                if not any(
                    _marker_covers_segment(marker, seg_start_local, seg_start_local + duration_ticks)
                    for seg_start_local, duration_ticks, _local_index in reference_entries
                ):
                    continue
                duration_attr = (
                    f' duration="{marker["segmentation_duration_ticks"]}"'
                    if marker.get("segmentation_duration_ticks") is not None
                    else ""
                )
                event_id_hex = event_id_map.get(marker["event_id"], marker["event_id"])
                event_id_dec = int(event_id_hex, 16)
                # [markers].dash_descriptor_mode: "shared" (default) embeds
                # the same full multi-descriptor message every coincident
                # marker carries; "narrowed" embeds the per-event,
                # single-descriptor re-encode instead -- independent of
                # whatever daterange_mode HLS is using (see bake.py's
                # DecodedMarker.splice_command_b64_narrowed).
                base_b64 = (
                    marker["splice_command_b64_narrowed"]
                    if pkg.dash_descriptor_mode == "narrowed"
                    else marker["splice_command_b64"]
                )
                splice_command_b64 = (
                    reencode_event_ids(base_b64, event_id_map)
                    if pkg.increment_event_ids
                    else base_b64
                )
                if is_instant_segmentation(marker):
                    direction = "instant"
                elif is_out_marker(marker):
                    direction = "out"
                else:
                    direction = "in"
                synthetic_id = event_id_dec * 4 + DIRECTION_CODE[direction]
                if pkg.dash_signal_format == "xml":
                    full_xml = build_scte35_full_xml(splice_command_b64)
                    indented_xml = "\n".join(
                        f"        {line}" for line in full_xml.splitlines()
                    )
                    signal_xml = (
                        f"      <scte35:Signal>\n"
                        f"{indented_xml}\n"
                        f"      </scte35:Signal>"
                    )
                else:
                    signal_xml = (
                        f"      <scte35:Signal>\n"
                        f"        <scte35:Binary>{splice_command_b64}</scte35:Binary>\n"
                        f"      </scte35:Signal>"
                    )
                # Period-relative, like <S t=...> -- subtract this Period's
                # own start tick (grave-robber/SCOPE.md §6.2's declared
                # offset lives in the Period's start= instead, see above; a
                # no-op subtraction of pkg.segment_boundary_ticks[0]==0 in
                # the common single-span-per-loop case).
                event_presentation_time = (
                    marker["pts_time_ticks"] - pkg.segment_boundary_ticks[span_start_local] - pto_ticks
                )
                event_xml.append((event_presentation_time,
                    _dash_event_comment(
                        (marker, event_id_map, None, self._marker_start_iso(marker, loop_number))
                    )
                    + f'    <Event presentationTime="{event_presentation_time}"'
                    f'{duration_attr} id="{synthetic_id}">\n'
                    f"{signal_xml}\n"
                    f"    </Event>"
                ))

            time_addressing = self.dash_addressing == "time"
            start_number_attr = ("" if time_addressing else f' startNumber="{first_number}"') + self.ll_template_attrs
            audio_media = (
                f"audio/seg/t/{real_span_index}/$Time$.m4s" if time_addressing else "audio/seg/$Number$.m4s"
            )

            video_representations = []
            for rendition in pkg.video_renditions:
                video_media = (
                    f"{rendition.name}/seg/t/{real_span_index}/$Time$.m4s"
                    if time_addressing
                    else f"{rendition.name}/seg/$Number$.m4s"
                )
                entries = _period_entries(
                    rendition.segment_boundary_ticks, local_indices, origin_local
                )
                timeline_lines = _segment_timeline_xml(entries)
                pto_attr = _extra_pto_attr(rendition.segment_boundary_ticks, pto_ticks)
                v = rendition.video_variant
                # A sparse (self-initializing) rendition's segments carry
                # their own moov (SCOPE.md §11) -- there's no shared init
                # segment to point `initialization=` at, same reasoning as
                # HLS's #EXT-X-MAP omission above.
                if rendition.self_initializing:
                    init_attr = ""
                elif rendition.shared_init:
                    init_attr = f' initialization="{rendition.name}/init_{real_span_index}.mp4"'
                else:
                    init_attr = f' initialization="{rendition.name}/init.mp4"'

                video_representations.append(
                    {
                        "name": rendition.name,
                        "variant": v,
                        "media": video_media,
                        "init_attr": init_attr,
                        "timeline": timeline_lines,
                        "pto_attr": pto_attr,
                    }
                )

            audio_adaptation_set = ""
            if pkg.has_audio:
                a = pkg.audio_rendition.audio_variant
                audio_init = (
                    f"audio/init_{real_span_index}.mp4" if pkg.audio_rendition.audio_sparse else "audio/init.mp4"
                )
                audio_entries = _period_entries(
                    pkg.audio_rendition.audio_segment_boundary_ticks, local_indices, origin_local
                )
                audio_pto_attr = _extra_pto_attr(
                    pkg.audio_rendition.audio_segment_boundary_ticks, pto_ticks
                )
                audio_timeline_lines = _segment_timeline_xml(audio_entries)
                audio_adaptation_set = render_audio_adaptation_set(
                    a, audio_media, audio_init, audio_timeline_lines, pkg.timescale,
                    start_number_attr, audio_pto_attr, self.dash_timeline == "compact",
                )

            # With more than one Period per loop (grave-robber/SCOPE.md §6.1), give each
            # Period's EventStream its own @value so dash.js's (value, id) dedupe key
            # can't fold events from two Periods of the same loop together.
            stream_value_attr = f' value="{loop_number}-{span_index}"' if spans_per_loop > 1 else ""
            event_streams_xml = (
                f'    <EventStream schemeIdUri="{scheme_id_uri}" timescale="{pkg.timescale}"{stream_value_attr}>\n'
                + _join_events(event_xml)
                + "\n    </EventStream>"
                if event_xml else ""
            )

            period_id = self.dash_period_id(loop_number, span_start_local)
            period_xml_parts.append(f'''  <Period id="{period_id}" start="PT{period_start_seconds}S">
{event_streams_xml}
    <AdaptationSet mimeType="video/mp4" segmentAlignment="true" startWithSAP="1">
{render_video_representations(video_representations, pkg.timescale, start_number_attr, self.dash_timeline == 'compact')}
    </AdaptationSet>{audio_adaptation_set}
  </Period>''')

        # @suggestedPresentationDelay tells compliant players to deliberately
        # stay this far behind the true live edge, rather than chasing it as
        # closely as possible. Without it, aggressive live-catchup players
        # (e.g. dash.js's default CatchupController) can outrun the actual
        # availability of the newest segment -- there is always up to one
        # segment-duration's worth of "not yet aired" content sitting right
        # at the live edge for any live stream (looping or not), since a
        # segment can't be advertised before its content has actually
        # occurred in wall-clock time. Observed effect without this: players
        # occasionally ran into that always-there edge gap and invoked their
        # own "jump the gap" recovery instead of just comfortably waiting
        # behind it. Two segment durations of delay margin is a
        # conservative, standard buffer for this.
        mpd_open = self._mpd_open_tag(
            window=window,
            window_segments=window_segments,
            origin_abs_ticks=window_origin_abs,
            duration_ticks=window_duration_ticks,
        )
        header_comments = "\n".join(f"<!-- {c} -->" for c in self._header_comments(window))
        mpd = f'''<?xml version="1.0" encoding="utf-8"?>
<!-- {constants.GENERATOR_COMMENT} -->
{header_comments}
{mpd_open}
{self._service_description_xml(window)}{chr(10).join(period_xml_parts)}
{self._utc_timing_xml(window)}</MPD>
'''
        return mpd

    def _build_dash_manifest_continuous(
        self, window_segments: int | None = None, window: TimeWindow | None = None
    ) -> str:
        """SCOPE.md §12.3: continuity mode's DASH output is ONE Period that
        never restarts -- its <SegmentTimeline> `t` values are each
        segment's real ABSOLUTE tick position (`loop_number *
        total_loop_duration_ticks` + its loop-relative tick), continuously
        increasing across every loop wrap, matching what the byte-serving
        routes actually rewrite the segments' own `tfdt` to. This is the
        DASH-side equivalent of the default mode's per-loop <Period> restart
        (see build_dash_manifest's own docstring) -- continuity mode's whole
        point is that there is no discontinuity left to give a Period
        boundary to.

        Only reached for a package that already passed
        `Channel._validate_continuous` (boundaries == {0}) -- so, unlike
        build_dash_manifest, there is exactly one span per loop. A sparse
        rendition (SCOPE.md §11) is allowed through as long as it has no
        internal joins, so `init_attr`/`audio_init` below still need to
        pick the right init-segment shape (self-initializing / per-span /
        shared) -- span index is always 0 when there's only one span.
        """
        window_segments = window_segments or self.window_segments
        pkg = self.package
        # SCOPE.md §13: with an explicit range, presentation time 0 is the
        # first segment (presentationTimeOffset below), t values stay absolute
        # so they match the tfdt the cseg route writes.
        win = self.window_range(window, window_segments)
        first_global_index, media_sequence = win.first, win.last
        global_indices = list(range(first_global_index, media_sequence + 1))
        first_loop, first_local = divmod(first_global_index, pkg.segments_per_loop)
        last_loop, last_local = divmod(media_sequence, pkg.segments_per_loop)
        origin_abs = (
            first_loop * pkg.total_loop_duration_ticks + pkg.segment_boundary_ticks[first_local]
            if window is not None
            else 0
        )
        end_abs = last_loop * pkg.total_loop_duration_ticks + (
            pkg.segment_boundary_ticks[last_local + 1]
            if last_local + 1 < len(pkg.segment_boundary_ticks)
            else pkg.total_loop_duration_ticks
        )
        spl = pkg.segments_per_loop

        # (governing break or None, [global indices]) -- one Period each.
        groups = [
            (brk, list(range(g_first, g_last + 1)))
            for brk, g_first, g_last in self.dash_continuous_groups(first_global_index, media_sequence)
        ]

        def _abs_ticks(global_index: int) -> int:
            loop_number, local_index = divmod(global_index, spl)
            return loop_number * pkg.total_loop_duration_ticks + pkg.segment_boundary_ticks[local_index]

        def _group_start_abs(brk: int | None) -> int:
            return 0 if brk is None else _abs_ticks(brk)

        def _group_start_rel(group_number: int, brk: int | None) -> int:
            """Period@start in ticks. A range rebases its first Period to 0
            (presentationTimeOffset below carries the lead-in)."""
            if window is not None:
                return 0 if group_number == 0 else _group_start_abs(brk) - origin_abs
            return _group_start_abs(brk)

        def _group_pto(group_number: int, brk: int | None) -> int:
            """Media time (absolute `t`/tfdt axis) that sits at Period@start,
            so presentation time stays the segments' own absolute position
            in every Period (timestamps never restart, only the signal)."""
            if window is not None and group_number == 0:
                return origin_abs
            return _group_start_abs(brk)

        def _entries(boundary_ticks: list[int], indices: list[int]) -> list[tuple[int, int, int]]:
            """(absolute_start_ticks, duration_ticks, local_index) -- `t`
            stays the absolute media position in every Period."""
            entries = []
            for global_index in indices:
                loop_number, local_index = divmod(global_index, spl)
                start = boundary_ticks[local_index]
                end = (
                    boundary_ticks[local_index + 1]
                    if local_index + 1 < len(boundary_ticks)
                    # The first segment need not start at tick zero (AAC
                    # typically starts a frame later than video). Its next
                    # iteration starts at loop_duration + boundary_ticks[0],
                    # not at loop_duration. Otherwise the MPD describes an
                    # audio gap at every wrap even though the shifted tfdt
                    # and media samples continue through it. dash.js can
                    # stop requesting audio at that gap and stall playback.
                    else pkg.total_loop_duration_ticks + boundary_ticks[0]
                )
                entries.append(
                    (loop_number * pkg.total_loop_duration_ticks + start, end - start, local_index)
                )
            return entries

        def _segment_timeline_xml(entries: list[tuple[int, int, int]]) -> str:
            # An XML comment naming the loop iteration before the first
            # segment of the Period and of each subsequent loop wrap inside it.
            items: list[tuple] = []
            for n, (t, d, local_index) in enumerate(entries):
                if local_index == 0 or n == 0:
                    items.append(("c", f"loop {self._loop_label(t // pkg.total_loop_duration_ticks)}"))
                items.append(("s", t, d))
            return render_segment_timeline(items, self.dash_timeline == "compact")

        group_of_global = {
            g: group_number for group_number, (_brk, indices) in enumerate(groups) for g in indices
        }

        # Markers: one <Event> per marker occurrence (per loop) whose interval
        # overlaps the window, placed in the Period of the first segment that
        # covers it (Channel.marker_anchors, the same placement HLS uses).
        DIRECTION_CODE = {"out": 0, "in": 1, "instant": 2}
        scheme_id_uri = (
            SCTE35_XML_NAMESPACE if pkg.dash_signal_format == "xml" else "urn:scte:scte35:2014:xml+bin"
        )
        event_xml_by_group: list[list[tuple[int, str]]] = [[] for _ in groups]
        anchors = self.marker_anchors(first_global_index, media_sequence)
        for global_index in global_indices:
            loop_number = global_index // spl
            group_number = group_of_global[global_index]
            event_id_map = build_event_id_map(pkg.markers, loop_number) if pkg.increment_event_ids else {}
            for marker in anchors.get(global_index, []):
                duration_attr = (
                    f' duration="{marker["segmentation_duration_ticks"]}"'
                    if marker.get("segmentation_duration_ticks") is not None
                    else ""
                )
                event_id_hex = event_id_map.get(marker["event_id"], marker["event_id"])
                event_id_dec = int(event_id_hex, 16)
                base_b64 = (
                    marker["splice_command_b64_narrowed"]
                    if pkg.dash_descriptor_mode == "narrowed"
                    else marker["splice_command_b64"]
                )
                splice_command_b64 = (
                    reencode_event_ids(base_b64, event_id_map) if pkg.increment_event_ids else base_b64
                )
                if is_instant_segmentation(marker):
                    direction = "instant"
                elif is_out_marker(marker):
                    direction = "out"
                else:
                    direction = "in"
                if pkg.dash_signal_format == "xml":
                    full_xml = build_scte35_full_xml(splice_command_b64)
                    indented_xml = "\n".join(f"        {line}" for line in full_xml.splitlines())
                    signal_xml = f"      <scte35:Signal>\n{indented_xml}\n      </scte35:Signal>"
                else:
                    signal_xml = (
                        f"      <scte35:Signal>\n"
                        f"        <scte35:Binary>{splice_command_b64}</scte35:Binary>\n"
                        f"      </scte35:Signal>"
                    )
                # A recurring marker gets a distinct id per loop_number it's
                # re-emitted at (a single ever-open Period, unlike the
                # default mode's one-namespace-per-Period, needs this to
                # avoid dash.js's EventController silently dropping a
                # same-id "duplicate" -- see build_dash_manifest's own
                # DIRECTION_CODE comment for the base id math). Only the
                # low bit of loop_number is folded in: a DVR window
                # (SCOPE.md §12) spanning more than two loop iterations at
                # once would need more room than this, but that is already
                # a pathological window/loop-duration combination, not
                # something continuity mode introduces.
                base_id = event_id_dec * 4 + DIRECTION_CODE[direction]
                if window is None:
                    synthetic_id = base_id * 2 + (loop_number & 1)
                else:
                    # A range can span many loops and must keep stable ids
                    # as a growing manifest is refreshed: fold in the low 16
                    # bits of the absolute loop number (SCOPE.md §13.6).
                    synthetic_id = (base_id << 16) | (loop_number & 0xFFFF)
                # Relative to the Period's own start (presentation axis).
                event_presentation_time = (
                    loop_number * pkg.total_loop_duration_ticks + marker["pts_time_ticks"]
                    - origin_abs - _group_start_rel(group_number, groups[group_number][0])
                )
                event_xml_by_group[group_number].append((event_presentation_time,
                    _dash_event_comment(
                        (marker, event_id_map, loop_number, self._marker_start_iso(marker, loop_number))
                    )
                    + f'    <Event presentationTime="{event_presentation_time}"'
                    f'{duration_attr} id="{synthetic_id}">\n'
                    f"{signal_xml}\n"
                    f"    </Event>"
                ))

        period_xml_parts = []
        for group_number, (brk, indices) in enumerate(groups):
            event_xml = event_xml_by_group[group_number]
            event_streams_xml = (
                f'    <EventStream schemeIdUri="{scheme_id_uri}" timescale="{pkg.timescale}">\n'
                + _join_events(event_xml)
                + "\n    </EventStream>"
                if event_xml else ""
            )
            start_rel = _group_start_rel(group_number, brk)
            pto = _group_pto(group_number, brk)
            pto_attr = f' presentationTimeOffset="{pto}"' if pto else ""
            first_global = indices[0]

            time_addressing = self.dash_addressing == "time"
            start_number_attr = ("" if time_addressing else f' startNumber="{first_global}"') + self.ll_template_attrs
            audio_media = "audio/cseg/t/$Time$.m4s" if time_addressing else "audio/cseg/$Number$.m4s"

            video_representations = []
            for rendition in pkg.video_renditions:
                video_media = (
                    f"{rendition.name}/cseg/t/$Time$.m4s"
                    if time_addressing
                    else f"{rendition.name}/cseg/$Number$.m4s"
                )
                timeline_lines = _segment_timeline_xml(_entries(rendition.segment_boundary_ticks, indices))
                v = rendition.video_variant
                # SCOPE.md §12.6: a sparse rendition may be self-initializing
                # (own moov, no shared init at all) or carry a per-span init
                # (span always 0 here, since boundaries == {0} means one span)
                # -- same three-way choice build_dash_manifest's own
                # per-Period builder makes, just with span_index fixed at 0.
                if rendition.self_initializing:
                    init_attr = ""
                elif rendition.shared_init:
                    init_attr = f' initialization="{rendition.name}/init_0.mp4"'
                else:
                    init_attr = f' initialization="{rendition.name}/init.mp4"'
                video_representations.append(
                    {
                        "name": rendition.name,
                        "variant": v,
                        "media": video_media,
                        "init_attr": init_attr,
                        "timeline": timeline_lines,
                        "pto_attr": pto_attr,
                    }
                )

            audio_adaptation_set = ""
            if pkg.has_audio:
                a = pkg.audio_rendition.audio_variant
                audio_timeline_lines = _segment_timeline_xml(
                    _entries(pkg.audio_rendition.audio_segment_boundary_ticks, indices)
                )
                # Same span-0 reasoning as video's init_attr above -- audio has
                # no self-initializing variant of its own (only sparse/init_N
                # vs. shared, see LoopPackage/VideoRendition).
                audio_init = (
                    "audio/init_0.mp4" if pkg.audio_rendition.audio_sparse else "audio/init.mp4"
                )
                audio_adaptation_set = render_audio_adaptation_set(
                    a, audio_media, audio_init, audio_timeline_lines, pkg.timescale,
                    start_number_attr, pto_attr, self.dash_timeline == "compact",
                )

            # Stable across polls: named after the break that opened it,
            # never after where the sliding window happens to clip it. With
            # no forced breaks this is the single never-restarted Period.
            period_id = self.dash_continuous_period_id(brk)
            start_seconds = ticks_to_wall_clock_seconds(start_rel, pkg.timescale)
            period_start = "PT0S" if start_rel == 0 else f"PT{start_seconds}S"
            period_xml_parts.append(f'''  <Period id="{period_id}" start="{period_start}">
{event_streams_xml}
    <AdaptationSet mimeType="video/mp4" segmentAlignment="true" startWithSAP="1">
{render_video_representations(video_representations, pkg.timescale, start_number_attr, self.dash_timeline == 'compact')}
    </AdaptationSet>{audio_adaptation_set}
  </Period>''')

        mpd_open = self._mpd_open_tag(
            window=window,
            window_segments=window_segments,
            origin_abs_ticks=origin_abs,
            duration_ticks=end_abs - origin_abs,
        )
        header_comments = "\n".join(f"<!-- {c} -->" for c in self._header_comments(window))
        mpd = f'''<?xml version="1.0" encoding="utf-8"?>
<!-- {constants.GENERATOR_COMMENT} -->
{header_comments}
{mpd_open}
{self._service_description_xml(window)}{chr(10).join(period_xml_parts)}
{self._utc_timing_xml(window)}</MPD>
'''
        return mpd
