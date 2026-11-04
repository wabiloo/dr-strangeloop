"""The shared timeline plan: window range, segment starts, marker anchors, Period grouping and URIs. Used
by the HLS and DASH builders AND by build_window_json, so the views cannot disagree about where the window
is, which segments are discontinuities, or which segment a marker is signaled on."""

from __future__ import annotations

import bisect

from loop_math import (
    compute_loop_position,
    global_segment_number,
    program_date_time_ticks,
    segment_index_for_position,
    ticks_to_wall_clock_seconds,
)
from timeshift import TimeWindow, resolve_window

from serving.markers import _marker_covers_segment
from serving.timeline import WindowRange


class TimelinePlanMixin:
    """The shared timeline plan: window range, segment starts, marker anchors, Period grouping and URIs. Used"""

    def segment_uris(self, first: int, last: int, window: TimeWindow | None) -> dict:
        """First/last video segment URI per format, for the reference
        rendition, relative to the manifest. Built from the same `segment_uri`
        the HLS playlist uses (periodic `/seg/<local>`, continuous
        `/cseg/<global>`); only the extension can differ between formats."""
        pkg, spl = self.package, self.package.segments_per_loop
        name = pkg.video_renditions[0].name
        hls_ext = "ts" if pkg.hls_format == "ts" else "m4s"
        hls_tpl = f"{name}/seg/{{index}}.{hls_ext}"
        dash_tpl = f"{name}/seg/{{index}}.m4s"

        def hls(g: int) -> str:
            return self.segment_uri(hls_tpl, g, g % spl, window)

        def dash(g: int) -> str:
            if self.dash_addressing == "time":
                return self.dash_time_segment_uri(name, pkg.video_renditions[0].segment_boundary_ticks, g)
            return self.segment_uri(dash_tpl, g, g % spl, window)

        return {
            "hls": {"first_uri": hls(first), "last_uri": hls(last)},
            "dash": {"first_uri": dash(first), "last_uri": dash(last)},
        }

    def real_span_index(self, local_index: int) -> int:
        return bisect.bisect_right(self.real_span_starts, local_index) - 1

    def dash_time_segment_uri(self, prefix: str, boundary_ticks: list[int], global_index: int) -> str:
        """`$Time$` addressing (--dash-addressing time): the segment URI whose
        trailing number is the segment's `<S t=...>` value. Continuity:
        `<prefix>/cseg/t/<t>` with t absolute (`loop*D + start`). Default
        mode: `<prefix>/seg/t/<real span>/<t>` with t relative to that span's
        start, as in the Period-relative timeline; the span is named in the
        path because t alone repeats from one span to the next."""
        pkg = self.package
        loop_number, local = divmod(global_index, pkg.segments_per_loop)
        if self.continuous:
            t = loop_number * pkg.total_loop_duration_ticks + boundary_ticks[local]
            return f"{prefix}/cseg/t/{t}.m4s"
        span = self.real_span_index(local)
        t = boundary_ticks[local] - boundary_ticks[self.real_span_starts[span]]
        return f"{prefix}/seg/t/{span}/{t}.m4s"

    def local_index_from_span_time(self, boundary_ticks: list[int], span: int, t: int) -> int | None:
        """Inverse of the default-mode `$Time$` URI: loop-local index of the
        segment at span-relative time `t`, or None if there is none."""
        if not 0 <= span < len(self.real_span_starts):
            return None
        origin = self.real_span_starts[span]
        target = boundary_ticks[origin] + t
        i = bisect.bisect_left(boundary_ticks, target)
        if i >= len(boundary_ticks) or boundary_ticks[i] != target or self.real_span_index(i) != span:
            return None
        return i

    def global_index_from_time(self, boundary_ticks: list[int], t: int) -> int | None:
        """Inverse of the continuity `$Time$` URI: global index of the segment
        whose absolute start is `t`, or None."""
        pkg = self.package
        loop_number, rem = divmod(t, pkg.total_loop_duration_ticks)
        i = bisect.bisect_left(boundary_ticks, rem)
        if i >= len(boundary_ticks) or boundary_ticks[i] != rem:
            return None
        return loop_number * pkg.segments_per_loop + i

    def segment_uri(
        self, template: str, global_index: int, local_index: int, window: TimeWindow | None
    ) -> str:
        """Segment URI for a manifest entry (SCOPE.md §13.5). `template`
        contains `/seg/{index}`. Default mode: `/seg/<local>`. Continuity:
        `/cseg/<global>`; continuity + HLS-TS + a time-shifted range:
        `/rseg/<origin_loop>/<global>` so timestamps are shifted relative to
        the range origin instead of the epoch (33-bit PTS wrap)."""
        if not self.continuous:
            return template.format(index=local_index)
        if window is not None and template.endswith(".ts"):
            return template.replace("/seg/", f"/rseg/{window.origin_loop}/").format(index=global_index)
        return template.replace("/seg/", "/cseg/").format(index=global_index)

    def resolve_timeshift(
        self,
        start_ticks: int,
        end_ticks: int | None,
        max_span_seconds: int,
        full_loop: bool,
    ) -> TimeWindow:
        pkg = self.package
        return resolve_window(
            start_ticks=start_ticks,
            end_ticks=end_ticks,
            now_ticks=self.now_ticks(),
            epoch_ticks=self.epoch_ticks,
            total_loop_duration_ticks=pkg.total_loop_duration_ticks,
            segment_boundary_ticks=pkg.segment_boundary_ticks,
            segments_per_loop=pkg.segments_per_loop,
            max_span_ticks=max_span_seconds * pkg.timescale,
            full_loop=full_loop,
        )

    def loop_number_and_local_index(self, global_index: int) -> tuple[int, int]:
        """(loop_number, local_index) decoded from a continuity-mode
        segment URL's index, which -- unlike the default mode's plain
        local/physical index -- is the ever-increasing global segment
        number (SCOPE.md §12.2), so the same physical bytes can be shifted
        by the right per-loop offset regardless of which loop iteration a
        given request actually belongs to."""
        return divmod(global_index, self.package.segments_per_loop)

    def continuity_shift_ticks(self, loop_number: int) -> int:
        return loop_number * self.package.total_loop_duration_ticks

    def window_range(
        self, window: TimeWindow | None = None, window_segments: int | None = None, now: int | None = None
    ) -> WindowRange:
        """The window's global segment range. Live: the last `window_segments`
        up to the live edge (the segment containing "now"; clamped to 0 in the
        first moments after start). Time-shifted: the resolved range."""
        pkg = self.package
        position = compute_loop_position(
            self.now_ticks() if now is None else now, self.epoch_ticks, pkg.total_loop_duration_ticks
        )
        live_edge = global_segment_number(
            position.loop_number,
            segment_index_for_position(position.position_in_loop_ticks, pkg.segment_boundary_ticks),
            pkg.segments_per_loop,
        )
        if window is not None:
            return WindowRange(window.first_global, window.last_global, live_edge, window.ended, position)
        count = window_segments or self.window_segments
        return WindowRange(max(0, live_edge - count + 1), live_edge, live_edge, False, position)

    def ref_segment_bounds(self, local_index: int) -> tuple[int, int]:
        """Loop-relative [start, end) ticks of a segment on the reference
        rendition's timeline -- the ad-decision authority for markers/assets."""
        boundaries = self.package.segment_boundary_ticks
        end = boundaries[local_index + 1] if local_index + 1 < len(boundaries) else self.package.total_loop_duration_ticks
        return boundaries[local_index], end

    def loop_tick(self, loop_number: int, local_ticks: int) -> int:
        """Absolute (PROGRAM-DATE-TIME / Period@start frame) tick of a
        loop-relative position: the loop's start plus the declared asset-gap
        offset of the segment containing it."""
        pkg = self.package
        index = segment_index_for_position(local_ticks, pkg.segment_boundary_ticks)
        return program_date_time_ticks(
            loop_number,
            local_ticks + pkg.declared_offset_ticks_by_local_index[index],
            pkg.total_loop_duration_ticks,
            self.epoch_ticks,
        )

    def segment_start_ticks(self, global_index: int, boundary_ticks: list[int] | None = None) -> int:
        """Absolute declared start tick of a segment (its EXT-X-PROGRAM-DATE-TIME),
        on `boundary_ticks`' timeline (default: the reference rendition's)."""
        pkg = self.package
        loop_number, local_index = divmod(global_index, pkg.segments_per_loop)
        start = (boundary_ticks or pkg.segment_boundary_ticks)[local_index]
        return program_date_time_ticks(
            loop_number,
            start + pkg.declared_offset_ticks_by_local_index[local_index],
            pkg.total_loop_duration_ticks,
            self.epoch_ticks,
        )

    def marker_anchors(self, first: int, last: int) -> dict[int, list[dict]]:
        """Markers signaled on each global segment of [first, last].

        A marker (one occurrence per loop) is described ONCE per response, on
        the earliest in-range segment its active interval overlaps (see
        `_marker_covers_segment`). Anchors therefore move forward as older
        segments leave the window, and vanish with the last overlapping one.
        Keyed per loop: the same marker recurs every loop. Segment -> markers
        in package order; segments with nothing to signal are absent.
        """
        markers = self.package.markers
        seen: set[tuple[int, str, object, int]] = set()
        anchors: dict[int, list[dict]] = {}
        for g in range(first, last + 1):
            loop_number, local_index = divmod(g, self.package.segments_per_loop)
            start, end = self.ref_segment_bounds(local_index)
            hits = []
            for m in markers:
                key = (loop_number, m["event_id"], m.get("marker_identity"), m["pts_time_ticks"])
                if key not in seen and _marker_covers_segment(m, start, end):
                    seen.add(key)
                    hits.append(m)
            if hits:
                anchors[g] = hits
        return anchors

    def dash_period_id(self, loop_number: int, span_start_local: int | None = None) -> str:
        """DASH Period@id. Periodic: `loop<N>`, or `loop<N>-<first segment>`
        when a loop holds several Periods. Continuous: `continuous` before the
        first forced break, then `break<global index of the break>`
        (`dash_continuous_period_id`)."""
        return f"loop{loop_number}" if len(self.dash_period_starts) == 1 else f"loop{loop_number}-{span_start_local}"

    @staticmethod
    def dash_continuous_period_id(brk: int | None) -> str:
        return "continuous" if brk is None else f"break{brk}"

    def dash_governing_break(self, global_index: int) -> int | None:
        """Continuous mode: global index of the forced break that opened the
        Period this segment belongs to (None: before the first one ever)."""
        breaks = sorted(self.signal_breaks)
        spl = self.package.segments_per_loop
        loop_number, local_index = divmod(global_index, spl)
        earlier = [b for b in breaks if b <= local_index]
        if earlier:
            return loop_number * spl + earlier[-1]
        if breaks and loop_number > 0:
            return (loop_number - 1) * spl + breaks[-1]
        return None

    def dash_next_break(self, brk: int | None) -> int | None:
        """The forced break that closes the Period opened by `brk` (None: it
        stays open, or there are no breaks)."""
        breaks = sorted(self.signal_breaks)
        if not breaks:
            return None
        if brk is None:
            return breaks[0]
        spl = self.package.segments_per_loop
        loop_number, local_index = divmod(brk, spl)
        later = [b for b in breaks if b > local_index]
        return loop_number * spl + later[0] if later else (loop_number + 1) * spl + breaks[0]

    def dash_continuous_groups(self, first: int, last: int) -> list[tuple[int | None, int, int]]:
        """Continuous mode: (governing break, first, last global segment) per
        DASH Period covering [first, last]."""
        groups: list[tuple[int | None, int, int]] = []
        for g in range(first, last + 1):
            brk = self.dash_governing_break(g)
            if groups and groups[-1][0] == brk:
                groups[-1] = (brk, groups[-1][1], g)
            else:
                groups.append((brk, g, g))
        return groups

    def dash_periodic_groups(self, first: int, last: int) -> list[tuple[int, int, int, int, int, int]]:
        """Periodic mode: (loop, span index, span start local, span end local
        (exclusive), first, last global segment) per DASH Period covering
        [first, last]. A Period is one (loop, span) run; first/last are
        clipped to the range, the span bounds are not."""
        spl = self.package.segments_per_loop
        starts = sorted(self.dash_period_starts)
        groups: list[tuple[int, int, int, int, int, int]] = []
        for g in range(first, last + 1):
            loop, local = divmod(g, spl)
            span = bisect.bisect_right(starts, local) - 1
            if groups and groups[-1][0] == loop and groups[-1][1] == span:
                groups[-1] = groups[-1][:5] + (g,)
            else:
                end = starts[span + 1] if span + 1 < len(starts) else spl
                groups.append((loop, span, starts[span], end, g, g))
        return groups

    def hls_discontinuities(self, first: int, last: int) -> list[int]:
        """Global indices in (first, last] that carry an #EXT-X-DISCONTINUITY.
        `first` never does: the header's DISCONTINUITY-SEQUENCE covers it."""
        starts = self.hls_period_starts
        if not starts:
            return []
        spl = self.package.segments_per_loop
        return [g for g in range(first + 1, last + 1) if g % spl in starts]

    def _loop_label(self, loop_number: int) -> str:
        """`<n> (starts <UTC wall-clock>)` -- the loop's real start time."""
        start = program_date_time_ticks(
            loop_number, 0, self.package.total_loop_duration_ticks, self.epoch_ticks
        )
        return f"{loop_number} (starts {self._iso_ticks(start)})"

    def _marker_start_iso(self, marker: dict, loop_number: int) -> str:
        """The marker's wall-clock start (ISO 8601, UTC) in the given loop,
        including any declared asset-gap offset, as DATERANGE START-DATE and
        the DASH Period start carry it."""
        return self._iso_ticks(self.loop_tick(loop_number, marker["pts_time_ticks"]))

    def _header_comments(self, window: TimeWindow | None) -> list[str]:
        """Comment text (no comment syntax) for the top of a manifest: live
        vs startover/catchup, and the time anchors loop start times derive
        from (the epoch, plus the clock offset if one is applied)."""
        out = []
        if self.channel_name:
            out.append(f"channel: {self.channel_name}")
        source_name = getattr(self.package, "source_name", "")
        if source_name:
            out.append(f"source: {source_name}")
        out += [
            "mode: live" if window is None else "mode: startover/catchup",
            f"continuous timeline: {'on' if self.continuous else 'off'}",
            f"epoch: {self._iso_ticks(self.epoch_ticks)}",
        ]
        if self.offset_ticks:
            out.append(
                f"clock offset: {ticks_to_wall_clock_seconds(self.offset_ticks, self.package.timescale)}s"
            )
        return out
