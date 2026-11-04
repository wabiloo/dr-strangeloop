"""The JSON view of the current window (/timeline.json)."""

from __future__ import annotations

from loop_math import segment_index_for_position, ticks_to_wall_clock_seconds
from scte35_signaling import build_event_id_map, is_instant_segmentation, is_out_marker, reencode_event_ids
from timeshift import TimeWindow

from serving.markers import _marker_durations_ticks
from serving.timeline import compute_discontinuity_sequence



class WindowJsonMixin:
    """The JSON view of the current window (/timeline.json)."""

    def build_window_json(self, window: "TimeWindow | None" = None, scope: str = "window") -> dict:
        """JSON view of the current window (openapi.yaml `WindowDocument`):
        periods, discontinuities, assets and markers overlapping the same
        segments the HLS media playlist advertises (trailing window ending
        at the live edge, or the resolved startover/catchup range).

        `scope="loops"` (live only) widens the returned *content* -- periods,
        discontinuities, assets, markers -- to whole loops: the previous, the
        current (holding the live edge) and the next one, extended further back
        if the window reaches before the previous loop. `window` still describes
        the manifest window and `range` the extent of the content.

        Window range, segment timing, discontinuities, Period grouping/ids and
        marker placement come from the same `Channel` helpers the HLS/DASH
        builders use (`window_range`, `segment_start_ticks`, `loop_tick`,
        `hls_discontinuities`, `dash_continuous_groups`, `dash_period_id`,
        `marker_anchors`). What stays JSON-specific: a marker/asset *span*
        (a splice_insert break also covers the time up to its IN) and the
        loops-scope widening. All timing is integer ticks until the final
        ISO/seconds conversion; segment timing uses the reference rendition.
        """
        pkg = self.package
        ts, D, spl = pkg.timescale, pkg.total_loop_duration_ticks, pkg.segments_per_loop
        boundary = pkg.segment_boundary_ticks
        now = self.now_ticks()
        win = self.window_range(window, now=now)
        pos, live_edge, first, last, ended = win.position, win.live_edge, win.first, win.last, win.ended
        w_first, w_last = first, last
        if scope == "loops" and window is None:
            cur = live_edge // spl
            first = min(max(cur - 1, 0), w_first // spl) * spl
            last = (cur + 2) * spl - 1
        else:
            scope = "window"

        def secs(t: int) -> float:
            return round(ticks_to_wall_clock_seconds(t, ts), 3)

        def seg_range(first_seg: int, last_seg: int) -> dict:
            return {
                "first": first_seg,
                "last": last_seg,
                "count": max(0, last_seg - first_seg + 1),
                **self.segment_uris(first_seg, last_seg, window),
            }

        def seg_start(g: int) -> int:
            return self.segment_start_ticks(g)

        def seg_end(g: int) -> int:
            start, end = self.ref_segment_bounds(g % spl)
            return seg_start(g) + end - start

        loop_tick = self.loop_tick

        def seg_of(loop: int, local_ticks: int) -> int:
            return loop * spl + segment_index_for_position(local_ticks, boundary)

        def overlaps(g_first: int, g_last: int) -> bool:
            return g_first <= last and g_last >= first

        loops = range(first // spl, last // spl + 1)

        # --- discontinuities (HLS view) -----------------------------------
        hls_starts = self.hls_period_starts
        discontinuities = []
        if hls_starts:
            for g in self.hls_discontinuities(first, last):
                local = g % spl
                if local in pkg.boundaries:
                    reason = "loop_wrap" if local == 0 else "asset_boundary"
                else:
                    reason = "signal_break"
                discontinuities.append(
                    {
                        "segment": g,
                        "utc": self._iso_ticks(seg_start(g)),
                        "reason": reason,
                        "sequence": compute_discontinuity_sequence(g, spl, hls_starts),
                    }
                )

        # --- periods (DASH view) ------------------------------------------
        periods = []
        if self.continuous:
            groups = self.dash_continuous_groups(first, last)
            for brk, g_first, g_last in groups:
                nxt = self.dash_next_break(brk)
                periods.append(
                    {
                        "id": self.dash_continuous_period_id(brk),
                        "start_utc": self._iso_ticks(seg_start(0 if brk is None else brk)),
                        "end_utc": None if nxt is None else self._iso_ticks(seg_start(nxt)),
                        "segments": seg_range(g_first, g_last),
                    }
                )
        else:
            for loop, _span, s, e, g_first, g_last in self.dash_periodic_groups(first, last):
                full_first, full_last = loop * spl + s, loop * spl + e - 1
                periods.append(
                    {
                        "id": self.dash_period_id(loop, s),
                        "start_utc": self._iso_ticks(seg_start(full_first)),
                        "end_utc": self._iso_ticks(seg_end(full_last)),
                        "segments": seg_range(g_first, g_last),
                    }
                )

        # --- assets --------------------------------------------------------
        assets = []
        ordered = sorted(pkg.asset_boundaries, key=lambda b: b["start_ticks"])
        for loop in loops:
            for i, b in enumerate(ordered):
                a_start = int(b["start_ticks"])
                a_end = int(ordered[i + 1]["start_ticks"]) if i + 1 < len(ordered) else D
                if a_end <= a_start:
                    continue
                g_first, g_last = seg_of(loop, a_start), seg_of(loop, a_end - 1)
                if not overlaps(g_first, g_last):
                    continue
                start_abs = loop_tick(loop, a_start)
                assets.append(
                    {
                        "asset_id": b["asset_id"],
                        "loop": loop,
                        "start_utc": self._iso_ticks(start_abs),
                        "end_utc": self._iso_ticks(start_abs + a_end - a_start),
                        "duration_s": secs(a_end - a_start),
                        "loop_start_s": secs(a_start),
                        "loop_end_s": secs(a_end),
                        "segments": seg_range(max(g_first, first), min(g_last, last)),
                        "starts_before_range": g_first < first,
                        "ends_after_range": g_last > last,
                    }
                )

        # --- markers -------------------------------------------------------
        durations = _marker_durations_ticks(pkg.markers)
        markers = []
        for loop in loops:
            id_map = build_event_id_map(pkg.markers, loop) if pkg.increment_event_ids else {}
            remapped = any(k != v for k, v in id_map.items())
            for m, dur in zip(pkg.markers, durations):
                pts = m["pts_time_ticks"]
                end_local = min(pts + dur, D) if dur else pts + 1
                g_first, g_last = seg_of(loop, pts), seg_of(loop, max(end_local - 1, pts))
                if not overlaps(g_first, g_last):
                    continue
                start_abs = loop_tick(loop, pts)
                event_id = id_map.get(m["event_id"], m["event_id"])
                b64 = (
                    m.get("splice_command_b64_narrowed") or m.get("splice_command_b64")
                    if pkg.daterange_mode == "narrowed"
                    else m.get("splice_command_b64")
                )
                if remapped and b64:
                    b64 = reencode_event_ids(b64, id_map)
                entry = {
                    "event_id": event_id,
                    "type": m.get("type"),
                    "splice_type": m.get("splice_type"),
                    "is_out": is_out_marker(m),
                    "is_instant": is_instant_segmentation(m),
                    "loop": loop,
                    "start_utc": self._iso_ticks(start_abs),
                    "end_utc": self._iso_ticks(start_abs + dur) if dur else None,
                    "duration_s": secs(dur) if dur else None,
                    "segmentation_type_id": m.get("segmentation_type_id"),
                    "upid_type": m.get("upid_type"),
                    "upid_hex": m.get("upid_hex"),
                    "unique_program_id": m.get("unique_program_id"),
                    "assets": list(m.get("assets") or []),
                    "segments": seg_range(max(g_first, first), min(g_last, last)),
                    "starts_before_range": g_first < first,
                    "ends_after_range": g_last > last,
                    "splice_command_b64": b64,
                }
                if event_id != m["event_id"]:
                    entry["original_event_id"] = m["event_id"]
                markers.append(entry)
        markers.sort(key=lambda e: (e["start_utc"], e["event_id"]))

        # --- renditions ----------------------------------------------------
        audio = pkg.audio_rendition.audio_variant if pkg.has_audio else None
        renditions = []
        for i, r in enumerate(pkg.video_renditions):
            v = r.video_variant
            renditions.append(
                {
                    "name": r.name,
                    "reference": i == 0,
                    "bandwidth": v["bandwidth"] + (audio["bandwidth"] if audio else 0),
                    "codecs": ",".join([v["codecs"], *([audio["codecs"]] if audio else [])]),
                    "width": v["width"],
                    "height": v["height"],
                    "frame_rate": v["frame_rate"],
                    "has_audio": pkg.has_audio,
                    "playlist": pkg.video_playlist_name(r),
                }
            )

        return {
            "version": 1,
            "channel": self.channel_name,
            "generated_at": self._iso_ticks(now),
            "mode": "live" if window is None else "timeshift",
            "timeline": "continuous" if self.continuous else "periodic",
            "clock_offset_s": secs(self.offset_ticks),
            "timescale": ts,
            "epoch_utc": self._iso_ticks(self.epoch_ticks),
            "loop": {
                "number": pos.loop_number,
                "position_s": secs(pos.position_in_loop_ticks),
                "duration_s": secs(D),
                "segments": spl,
                "segment_duration_s": pkg.segment_duration_seconds,
            },
            "window": {
                "ended": ended,
                "start_utc": self._iso_ticks(seg_start(w_first)),
                "end_utc": self._iso_ticks(seg_end(w_last)),
                "live_edge_utc": None if ended else self._iso_ticks(seg_start(w_last)),
                "duration_s": secs(seg_end(w_last) - seg_start(w_first)),
                "segments": seg_range(w_first, w_last),
            },
            "range": {
                "scope": scope,
                "start_utc": self._iso_ticks(seg_start(first)),
                "end_utc": self._iso_ticks(seg_end(last)),
                "duration_s": secs(seg_end(last) - seg_start(first)),
                "segments": seg_range(first, last),
            },
            "loops": [
                {
                    "number": n,
                    "start_utc": self._iso_ticks(seg_start(n * spl)),
                    "end_utc": self._iso_ticks(seg_end(n * spl + spl - 1)),
                    "segments": seg_range(n * spl, n * spl + spl - 1),
                    "current": window is None and n == live_edge // spl,
                }
                for n in loops
            ],
            "renditions": renditions,
            "periods": periods,
            "discontinuities": discontinuities,
            "assets": assets,
            "markers": markers,
        }
