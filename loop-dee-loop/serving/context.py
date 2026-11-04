"""The state every route shares, and the request helpers built on it."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from flask import abort, request, Response

import continuity
import package_io
from serving.channel import Channel
from serving.httputil import _send_package_file
from serving.ll_http import LowLatencyHttpMixin
from serving.package import LoopPackage
from serving.query import resolve_request_channel
from timeshift import (
    FULL_LOOPS_PARAM,
    parse_bool,
    parse_instant_ticks,
    TimeshiftConfig,
    TimeshiftError,
    TimeWindow,
)


# Flask `defaults=` for the plain and the continuous segment URL forms (SCOPE.md §13.5).
SEG_DEFAULTS = {"mode": "local", "origin_loop": None}


class ServeContext(LowLatencyHttpMixin):
    """What every route shares: the loop package, the channel over it, the timeshift/override settings, and the
    request helpers (resolving a request to its channel variant, serving segment bytes, decoding segment URLs).
    One per Flask app, built once at startup; routes only read it."""

    def __init__(
        self,
        package_dir: Path,
        epoch_ticks: int,
        window_segments: int = 6,
        continuous: bool = False,
        timeshift: TimeshiftConfig | None = None,
        period_on_segmentation: frozenset[int] = frozenset(),
        channel_name: str = "",
        dash_addressing: str = "number",
        dash_timeline: str = "full",
        ll_dash: bool = False,
        ll_target_latency: float | None = None,
        ll_utc_timing_url: str = "",
        ll_utc_timing: str = "direct",
        ll_optin: bool = False,
        ll_hls: bool = False,
    ):
        self.package_dir = package_dir
        self.epoch_ticks = epoch_ticks
        self.ts_cfg = timeshift or TimeshiftConfig()
        self.package = LoopPackage(package_dir)
        self.channel = Channel(
            self.package,
            epoch_ticks,
            window_segments=window_segments,
            continuous=continuous,
            period_on_segmentation=period_on_segmentation,
            channel_name=channel_name,
            dash_addressing=dash_addressing,
            dash_timeline=dash_timeline,
            ll_dash=ll_dash,
            ll_target_latency=ll_target_latency,
            ll_utc_timing_url=ll_utc_timing_url,
            ll_utc_timing=ll_utc_timing,
            ll_optin=ll_optin,
            ll_hls=ll_hls,
        )
        self.process_start_ticks = self.channel.now_ticks()

    def request_view(self, fmt: str = "hls") -> tuple[Channel, TimeWindow | None, str]:
        """SCOPE.md §13: resolve this request's (channel variant, time-shift
        window, query string to propagate to child playlists). Without
        timeshift enabled -- or without the params -- this is (channel,
        None, "") and behavior is exactly the pre-§13 live stream."""
        ch, window = self.channel, None
        if self.ts_cfg.active:
            args = request.args
            try:
                ch = resolve_request_channel(self.channel, args, self.ts_cfg, fmt)
                raw_start = args.get(self.ts_cfg.start_name) if self.ts_cfg.timeshift else None
                raw_end = args.get(self.ts_cfg.end_name) if self.ts_cfg.timeshift else None
                if raw_start is None and raw_end is not None:
                    raise TimeshiftError(f"'{self.ts_cfg.end_name}' requires '{self.ts_cfg.start_name}'")
                if raw_start is not None:
                    ts = self.package.timescale
                    window = ch.resolve_timeshift(
                        parse_instant_ticks(raw_start, ts, self.ts_cfg.start_name),
                        parse_instant_ticks(raw_end, ts, self.ts_cfg.end_name) if raw_end is not None else None,
                        self.ts_cfg.max_span_seconds,
                        parse_bool(args.get(self.ts_cfg.name(FULL_LOOPS_PARAM), "false"), self.ts_cfg.name(FULL_LOOPS_PARAM)),
                    )
            except TimeshiftError as exc:
                abort(Response(f"{exc}\n", status=400, mimetype="text/plain"))
            query = urlencode([(k, args[k]) for k in self.ts_cfg.param_names if k in args])
        else:
            query = ""
        return ch, window, query

    def manifest_response(self, body: str, mimetype: str, window: TimeWindow | None) -> Response:
        resp = Response(body, mimetype=mimetype)
        if window is not None:
            # An ended range never changes again; a growing one changes once
            # per segment. Live responses keep whatever caching they had.
            resp.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
                if window.ended
                else f"public, max-age={self.package.max_segment_duration_seconds_rounded_up}"
            )
        return resp

    def decode_segment_index(self, index: int, mode: str, origin_loop: int | None) -> tuple[int, int | None]:
        """(local_index, shift_ticks) for a segment URL (SCOPE.md §13.5):
        `seg` -> index is the local one, bytes untouched; `cseg` -> index is
        global, shift by loop*D; `rseg` -> global, shift relative to
        `origin_loop`."""
        if mode == "local":
            return index, None
        try:
            ch = self.channel.for_mode(True)
        except TimeshiftError:
            abort(404)
        loop_number, local_index = ch.loop_number_and_local_index(index)
        if mode == "origin":
            if origin_loop is None or loop_number < origin_loop:
                abort(404)
            return local_index, ch.continuity_shift_ticks(loop_number - origin_loop)
        return local_index, ch.continuity_shift_ticks(loop_number)

    def serve_segment(
        self,
        path: Path | None,
        mimetype: str,
        *,
        ts: bool,
        shift_ticks: int | None,
        sequence_number: int = 0,
        pace: "tuple[int, int, int] | None" = None,
    ):
        """Shared byte-serving tail for every segment route (SCOPE.md §12):
        the untouched static-file response in default mode, or a per-
        request continuity.py patch (never re-muxed, just the fixed-width
        timestamp fields rewritten) when `shift_ticks` is not None."""
        if path is None:
            abort(404)
        if shift_ticks is None and pace is None:
            return _send_package_file(path, mimetype)
        data = package_io.read_bytes(path)
        if shift_ticks is not None:
            data = (
                continuity.shift_ts_segment(data, shift_ticks)
                if ts
                else continuity.shift_cmaf_fragment(data, shift_ticks, sequence_number=sequence_number)
            )
        if pace is not None:
            return self.paced_response(data, mimetype, *pace)
        return Response(data, mimetype=mimetype)
