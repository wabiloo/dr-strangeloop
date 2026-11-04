"""Serve phase entrypoint (SCOPE.md §4.2). Long-running, stateless per
request -- no in-memory "current position" variable that a timer advances.

    LINT / CODE-REVIEW CHECKLIST ITEM (SCOPE.md §5):
    No persisted or accumulated timing state in this module may be a Python
    `float`. Every persisted tick value is an `int`. Floats appear only as a
    final, one-shot conversion for display/serialization of a single
    response (e.g. an ISO8601 timestamp string) -- never fed back into a
    subsequent computation.

Every request independently derives the current position from `now()`
versus a fixed channel epoch via loop_math.compute_loop_position. Segment
bytes are served completely unmodified (same bytes every loop); only
manifest text differs per request.

Multi-rendition (ABR ladder): `loop_descriptor.json` (v2) carries a list of
`video_renditions`, each independently baked (bake.py) but sharing the same
`segments_per_loop` count and the same marker/timeline data. Exactly one
rendition also carries audio (see bake.py's `include_audio`) -- audio is
never duplicated per video rendition.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import math
import time
from pathlib import Path
from urllib.parse import urlencode

from flask import abort, Flask, jsonify, request, Response, send_file

import cmaf
import continuity
import lowlatency
import package_io
from timeshift import (
    FULL_LOOPS_PARAM,
    OFFSET_PARAM,
    parse_bool,
    parse_instant_ticks,
    TIMELINE_PARAM,
    TimeshiftConfig,
    TimeshiftError,
    TimeWindow,
)

from serving.channel import Channel
from serving.constants import (
    _DOCS_HTML,
    DASH_ADDRESSING_CHOICES,
    DASH_TIMELINE_CHOICES,
    LL_UTC_TIMING_CHOICES,
)
from serving.httputil import _is_loopback_host, _send_package_file
from serving.package import LoopPackage
from serving.query import resolve_request_channel
from serving.timeline import parse_segmentation_type_ids



# Every name serve.py used to define stays importable from here (tests, wsgi.py).
from serving.channel import Channel  # noqa: F401
from serving.constants import DASH_ADDRESSING_CHOICES, DASH_TIMELINE_CHOICES, GENERATOR_COMMENT, GENERATOR_VERSION, LL_HLS_PART_SEGMENTS, LL_UTC_TIMING_CHOICES, _DOCS_HTML, _read_generator_version  # noqa: F401
from serving.dash import DashMixin  # noqa: F401
from serving.dash_render import _dash_frame_rate, _join_events, render_audio_adaptation_set, render_segment_timeline, render_video_representations  # noqa: F401
from serving.hls import HlsMixin, _HlsLL, _part_uri  # noqa: F401
from serving.httputil import _is_loopback_host, _send_package_file  # noqa: F401
from serving.markers import _asset_ids_starting_in_segment, _dash_event_comment, _describe_markers, _hls_payload_markers, _marker_covers_segment, _marker_durations_ticks, _marker_type_code, _remap_signaling_markers  # noqa: F401
from serving.package import LoopPackage, VideoRendition, _cmaf_tfdt_version, _first_present_segment, _numeric_segment_index  # noqa: F401
from serving.plan import TimelinePlanMixin  # noqa: F401
from serving.query import resolve_request_channel  # noqa: F401
from serving.timeline import WindowRange, compute_asset_boundary_set, compute_declared_offset_ticks_by_local_index, compute_discontinuity_sequence, compute_signal_breaks, expand_segmentation_pairs, parse_segmentation_type_ids  # noqa: F401
from serving.variants import VariantsMixin  # noqa: F401
from serving.window_json import WindowJsonMixin  # noqa: F401


logger = logging.getLogger(__name__)


def create_app(
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
) -> Flask:
    ts_cfg = timeshift or TimeshiftConfig()
    package = LoopPackage(package_dir)
    channel = Channel(
        package,
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
    process_start_ticks = channel.now_ticks()

    app = Flask(__name__)

    def _serve_segment(
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
            return _paced_response(data, mimetype, *pace)
        return Response(data, mimetype=mimetype)

    def _paced_response(data: bytes, mimetype: str, start_tick: int, duration_ticks: int, track_timescale: int):
        """Low-latency DASH: stream an in-progress segment chunk by chunk, each
        released at its own wall-clock end (lowlatency.py). Sent with chunked
        transfer encoding (no Content-Length), so a player reads each chunk as
        it arrives."""
        try:
            chunks = lowlatency.split_chunks(data)
        except ValueError:
            return Response(data, mimetype=mimetype)  # not chunked: serve whole
        offsets = lowlatency.release_offsets_ticks(
            [t for _, t in chunks], track_timescale, package.timescale, duration_ticks
        )
        moof = cmaf._find(data, ("moof",))
        bodies = [c for c, _ in chunks]
        if moof and moof[0]:
            bodies[0] = data[: moof[0]] + bodies[0]  # styp/sidx ahead of the first chunk
        stream = lowlatency.paced(
            bodies, [start_tick + o for o in offsets], channel.now_ticks, time.sleep, package.timescale
        )
        resp = Response(stream, mimetype=mimetype)
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    _track_timescales: dict[str, int] = {}

    def _video_timescale(rendition) -> int:
        if rendition.name not in _track_timescales:
            found = None
            if not rendition.self_initializing:
                try:
                    found = cmaf.track_timescale(package_io.read_bytes(rendition.init_path()))
                except (OSError, RuntimeError):
                    found = None
            _track_timescales[rendition.name] = found or package.timescale
        return _track_timescales[rendition.name]

    def _ll_pace(boundaries: list[int], local_index: int, track_timescale: int, mode: str, index: int):
        """(start_tick, duration_ticks, track_timescale) when this segment is
        still in progress on the wall clock (low-latency DASH only), else None."""
        if not channel.ll_capable or local_index >= len(boundaries):
            return None
        end = boundaries[local_index + 1] if local_index + 1 < len(boundaries) else package.total_loop_duration_ticks
        duration = end - boundaries[local_index]
        start = lowlatency.segment_occurrence(
            channel.now_ticks(),
            epoch_ticks,
            package.total_loop_duration_ticks,
            boundaries[local_index],
            duration,
            round(package.segment_duration_seconds * package.timescale),
            loop_number=index // package.segments_per_loop if mode == "global" else None,
        )
        return None if start is None else (start, duration, track_timescale)

    @app.after_request
    def _add_cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    def _request_view(fmt: str = "hls") -> tuple[Channel, TimeWindow | None, str]:
        """SCOPE.md §13: resolve this request's (channel variant, time-shift
        window, query string to propagate to child playlists). Without
        timeshift enabled -- or without the params -- this is (channel,
        None, "") and behavior is exactly the pre-§13 live stream."""
        ch, window = channel, None
        if ts_cfg.active:
            args = request.args
            try:
                ch = resolve_request_channel(channel, args, ts_cfg, fmt)
                raw_start = args.get(ts_cfg.start_name) if ts_cfg.timeshift else None
                raw_end = args.get(ts_cfg.end_name) if ts_cfg.timeshift else None
                if raw_start is None and raw_end is not None:
                    raise TimeshiftError(f"'{ts_cfg.end_name}' requires '{ts_cfg.start_name}'")
                if raw_start is not None:
                    ts = package.timescale
                    window = ch.resolve_timeshift(
                        parse_instant_ticks(raw_start, ts, ts_cfg.start_name),
                        parse_instant_ticks(raw_end, ts, ts_cfg.end_name) if raw_end is not None else None,
                        ts_cfg.max_span_seconds,
                        parse_bool(args.get(ts_cfg.name(FULL_LOOPS_PARAM), "false"), ts_cfg.name(FULL_LOOPS_PARAM)),
                    )
            except TimeshiftError as exc:
                abort(Response(f"{exc}\n", status=400, mimetype="text/plain"))
            query = urlencode([(k, args[k]) for k in ts_cfg.param_names if k in args])
        else:
            query = ""
        return ch, window, query

    def _manifest_response(body: str, mimetype: str, window: TimeWindow | None) -> Response:
        resp = Response(body, mimetype=mimetype)
        if window is not None:
            # An ended range never changes again; a growing one changes once
            # per segment. Live responses keep whatever caching they had.
            resp.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
                if window.ended
                else f"public, max-age={package.max_segment_duration_seconds_rounded_up}"
            )
        return resp

    def _decode_segment_index(index: int, mode: str, origin_loop: int | None) -> tuple[int, int | None]:
        """(local_index, shift_ticks) for a segment URL (SCOPE.md §13.5):
        `seg` -> index is the local one, bytes untouched; `cseg` -> index is
        global, shift by loop*D; `rseg` -> global, shift relative to
        `origin_loop`."""
        if mode == "local":
            return index, None
        try:
            ch = channel.for_mode(True)
        except TimeshiftError:
            abort(404)
        loop_number, local_index = ch.loop_number_and_local_index(index)
        if mode == "origin":
            if origin_loop is None or loop_number < origin_loop:
                abort(404)
            return local_index, ch.continuity_shift_ticks(loop_number - origin_loop)
        return local_index, ch.continuity_shift_ticks(loop_number)

    @app.get("/health")
    def health():
        """Lightweight JSON status for external monitoring (e.g. a channel
        management UI). Not part of the HLS/DASH viewer-facing contract --
        safe to poll frequently, cheap (no file I/O beyond what's already
        loaded in memory at startup)."""
        pos = channel.current_position()
        now_ticks = channel.now_ticks()
        return {
            "status": "ok",
            "package_dir": str(package_dir),
            "timescale": package.timescale,
            "epoch_ticks": epoch_ticks,
            "total_loop_duration_ticks": package.total_loop_duration_ticks,
            "total_loop_duration_seconds": package.total_loop_duration_ticks / package.timescale,
            "loop_number": pos.loop_number,
            "position_in_loop_ticks": pos.position_in_loop_ticks,
            "position_in_loop_seconds": pos.position_in_loop_ticks / package.timescale,
            "uptime_seconds": (now_ticks - process_start_ticks) / package.timescale,
            "renditions": [r.name for r in package.video_renditions],
            "has_audio": package.has_audio,
            "hls_format": package.hls_format,
            "hls_ts_mux_audio": package.hls_ts_mux_audio,
            "window_segments": channel.window_segments,
            "continuous_timeline": channel.continuous,
            "epoch_utc": channel._iso_ticks(epoch_ticks),
            # The query-parameter configuration (no master "enabled": each
            # kind has its own switch; with both off `params` is empty).
            "query": {
                "timeshift": ts_cfg.timeshift,
                "overrides": ts_cfg.overrides,
                "prefix": ts_cfg.prefix,
                "start_param": ts_cfg.start_name,
                "end_param": ts_cfg.end_name,
                "timeline_param": ts_cfg.name(TIMELINE_PARAM),
                "offset_param": ts_cfg.name(OFFSET_PARAM),
                "full_loops_param": ts_cfg.name(FULL_LOOPS_PARAM),
                "params": list(ts_cfg.param_names),
                "max_span_seconds": ts_cfg.max_span_seconds,
                "continuous_supported": channel.continuous or channel.continuous_error is None,
            },
        }

    @app.get("/timeline.json")
    def window_json():
        """JSON view of the current window (periods, discontinuities, assets,
        markers); schema in openapi.yaml. Honours the same timeshift query
        params as the manifests."""
        ch, window, _query = _request_view("json")
        scope = request.args.get(ts_cfg.name("scope"), "window")
        if scope not in ("window", "loops"):
            abort(Response(f"'{ts_cfg.name('scope')}' must be 'window' or 'loops'\n", status=400, mimetype="text/plain"))
        resp = jsonify(ch.build_window_json(window, scope))
        resp.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if window is not None and window.ended else "public, max-age=1"
        )
        return resp

    @app.get("/openapi.yaml")
    def openapi_spec():
        return send_file(Path(__file__).with_name("openapi.yaml"), mimetype="application/yaml")

    @app.get("/docs")
    def api_docs():
        # Relative spec-url so it also resolves behind a path-prefixing proxy (igor).
        return Response(_DOCS_HTML, mimetype="text/html")

    @app.get(f"/{LoopPackage.INDEX_PLAYLIST}")
    @app.get(f"/{LoopPackage.HLS_ALIAS}")
    def hls_master_playlist():
        ch, window, query = _request_view()
        return _manifest_response(
            ch.build_hls_master_playlist(query), "application/vnd.apple.mpegurl", window
        )

    @app.get(f"/{LoopPackage.DASH_MANIFEST}")
    @app.get(f"/{LoopPackage.DASH_ALIAS}")
    def dash_manifest():
        ch, window, _query = _request_view("dash")
        if _is_loopback_host(request.host):
            # Only a request to this machine itself may name its own /time: any other Host header is
            # client-controlled, and a shared cache could keep a forged one for every viewer.
            ch = ch.with_ll_utc_timing_url(request.url_root + "time")
        resp = _manifest_response(ch.build_dash_manifest(window=window), "application/dash+xml", window)
        if ch.ll_dash and ch.ll_utc_timing == "direct" and window is None and ch.offset_ticks == 0:
            # The MPD carries the current time: a cached copy would be a wrong clock.
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/time")
    def server_time():
        """The channel clock as an xs:dateTime, for the low-latency MPD's
        `UTCTiming` (`urn:mpeg:dash:utc:http-xsdate:2014`). Never cached."""
        resp = Response(channel._iso_ticks(channel.now_ticks()), mimetype="text/plain")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _wait_until(release_tick: int) -> None:
        """Hold the request until the wall clock reaches `release_tick` (an LL-HLS part or segment becoming
        available), in short slices; gives up after three target durations, as a client would."""
        limit = channel.now_ticks() + 3 * package.max_segment_duration_seconds_rounded_up * package.timescale
        while True:
            now = channel.now_ticks()
            remaining = release_tick - now
            if remaining <= 0 or now >= limit:
                return
            time.sleep(min(remaining / package.timescale, 0.05))

    def _bad_request(message: str):
        abort(Response(f"{message}\n", status=400, mimetype="text/plain"))

    def _block_for_reload(ch: Channel, window, rendition_name: str | None) -> None:
        """LL-HLS blocking playlist reload: `_HLS_msn` (and `_HLS_part`) ask for a playlist that already holds that
        segment (part); hold the request until it exists. Ignored unless this is a low-latency live playlist.
        `_HLS_skip` (delta updates) is accepted and ignored: CAN-SKIP-UNTIL is not advertised."""
        raw_msn, raw_part = request.args.get("_HLS_msn"), request.args.get("_HLS_part")
        if raw_part is not None and raw_msn is None:
            _bad_request("'_HLS_part' needs '_HLS_msn'")
        if raw_msn is None or not ch.hls_ll_live(window):
            return
        try:
            msn = int(raw_msn)
            part = int(raw_part) if raw_part is not None else None
        except ValueError:
            _bad_request("'_HLS_msn' and '_HLS_part' must be non-negative integers")
        if msn < 0 or (part is not None and part < 0):
            _bad_request("'_HLS_msn' and '_HLS_part' must be non-negative integers")
        now = ch.now_ticks()
        if msn > ch.window_range(None, 1, now=now).last + 2:
            _bad_request("'_HLS_msn' is more than two segments ahead of the live edge")
        spl = package.segments_per_loop
        loop_number, local_index = divmod(msn, spl)
        if rendition_name is None:
            boundaries = package.audio_rendition.audio_segment_boundary_ticks
        else:
            boundaries = package.rendition_by_name(rendition_name).segment_boundary_ticks
        end = boundaries[local_index + 1] if local_index + 1 < len(boundaries) else package.total_loop_duration_ticks
        duration = end - boundaries[local_index]
        start = epoch_ticks + loop_number * package.total_loop_duration_ticks + boundaries[local_index]
        if part is None:
            release = start + duration  # the whole segment
        else:
            chunks = package.chunk_table(rendition_name, local_index, duration)
            if part >= len(chunks):
                _bad_request(f"'_HLS_part' is beyond the last part of segment {msn}")
            release = lowlatency.part_release_tick(chunks, start, part)
        _wait_until(release)

    def _register_video_playlist(rendition):
        def view():
            ch, window, query = _request_view()
            _block_for_reload(ch, window, rendition.name)
            body = ch.build_hls_manifest(rendition.name, window=window, query=query)
            return _manifest_response(body, "application/vnd.apple.mpegurl", window)

        name = package.video_playlist_name(rendition)
        app.add_url_rule(f"/{name}", endpoint=f"hls_{name}", view_func=view)

    for _rendition in package.video_renditions:
        _register_video_playlist(_rendition)

    @app.get("/<rendition_name>/init.mp4")
    def init_segment(rendition_name: str):
        try:
            rendition = package.rendition_by_name(rendition_name)
        except KeyError:
            abort(404)
        if rendition.self_initializing:
            # SCOPE.md §11: a sparse rendition's segments carry their own
            # moov -- there is no shared init to serve. The manifest never
            # advertises this URI in that case (see build_hls_manifest's
            # `no_init` handling), so a real player should never hit this;
            # 404 defensively rather than raising.
            abort(404)
        return _send_package_file(rendition.init_path())

    @app.get("/<rendition_name>/init_<int:span>.mp4")
    def span_init_segment(rendition_name: str, span: int):
        try:
            rendition = package.rendition_by_name(rendition_name)
        except KeyError:
            abort(404)
        path = rendition.init_path_for_span(span) if rendition.shared_init else None
        if path is None:
            abort(404)
        return _send_package_file(path)

    _SEG_DEFAULTS = {"mode": "local", "origin_loop": None}

    def _time_index(boundary_ticks: list[int], t: int, span: int | None) -> int:
        """Segment index named by a `$Time$` URL (--dash-addressing time): the
        loop-local one for `/seg/t/<span>/<t>`, the global one for
        `/cseg/t/<t>`. 404 when no segment of this track starts at `t`."""
        if span is None:
            try:
                ch = channel.for_mode(True)
            except TimeshiftError:
                abort(404)
            index = ch.global_index_from_time(boundary_ticks, t)
        else:
            index = channel.local_index_from_span_time(boundary_ticks, span, t)
        if index is None:
            abort(404)
        return index

    @app.get("/<rendition_name>/seg/t/<int:span>/<int:time>.m4s", defaults={"mode": "local", "origin_loop": None})
    @app.get("/<rendition_name>/cseg/t/<int:time>.m4s", defaults={"mode": "global", "origin_loop": None, "span": None})
    def segment_by_time(rendition_name: str, time: int, span: int | None, mode: str, origin_loop: int | None):
        try:
            ticks = package.rendition_by_name(rendition_name).segment_boundary_ticks
        except KeyError:
            abort(404)
        return segment(rendition_name, _time_index(ticks, time, span), mode, origin_loop)

    @app.get("/<rendition_name>/seg/<int:physical_index>.m4s", defaults=_SEG_DEFAULTS)
    @app.get("/<rendition_name>/cseg/<int:physical_index>.m4s", defaults={"mode": "global", "origin_loop": None})
    def segment(rendition_name: str, physical_index: int, mode: str, origin_loop: int | None):
        # SCOPE.md §13.5: /seg/ is always the loop-local physical index;
        # /cseg/ always the global index (continuity shift, §12.2).
        local_index, shift_ticks = _decode_segment_index(physical_index, mode, origin_loop)
        try:
            path = channel.segment_bytes_path(rendition_name, local_index)
        except (KeyError, IndexError):
            abort(404)
        # SCOPE.md §11.3: a sparse-mode index with no physical media 404s --
        # manifest generation is otherwise completely unaffected (the
        # manifest always advertised this segment as if it existed).
        rendition = package.rendition_by_name(rendition_name)
        pace = (
            _ll_pace(rendition.segment_boundary_ticks, local_index, _video_timescale(rendition), mode, physical_index)
            if channel.ll_capable
            else None
        )
        return _serve_segment(
            path, "video/iso.segment", ts=False, shift_ticks=shift_ticks, sequence_number=physical_index, pace=pace
        )

    def _serve_part(rendition_name: str | None, physical_index: int, part: int, mode: str, origin_loop: int | None):
        """One LL-HLS part (a baked chunk) of a segment, `rendition_name` None = audio. A part that is not complete
        yet is held until it is (the answer to a preload hint); a finished one is served at once."""
        if not channel.ll_capable or package.hls_format != "cmaf":
            abort(404)
        local_index, shift_ticks = _decode_segment_index(physical_index, mode, origin_loop)
        try:
            if rendition_name is None:
                audio = package.audio_rendition
                path = channel.audio_segment_bytes_path(local_index)
                boundaries, track_timescale, mimetype = audio.audio_segment_boundary_ticks, audio.audio_timescale(package.timescale), "audio/iso.segment"
                if shift_ticks is not None:  # tfdt is in the audio track's own timescale
                    shift_ticks = round(shift_ticks * track_timescale / package.timescale)
            else:
                rendition = package.rendition_by_name(rendition_name)
                path = channel.segment_bytes_path(rendition_name, local_index)
                boundaries, track_timescale, mimetype = rendition.segment_boundary_ticks, _video_timescale(rendition), "video/iso.segment"
        except (KeyError, IndexError):
            abort(404)
        if path is None:
            abort(404)
        end = boundaries[local_index + 1] if local_index + 1 < len(boundaries) else package.total_loop_duration_ticks
        duration = end - boundaries[local_index]
        chunks = package.chunk_table(rendition_name, local_index, duration)
        if part >= len(chunks):
            abort(404)
        pace = _ll_pace(boundaries, local_index, track_timescale, mode, physical_index)
        if pace is not None:
            _wait_until(lowlatency.part_release_tick(chunks, pace[0], part))
        data = package_io.read_bytes(path)
        if shift_ticks is not None:
            data = continuity.shift_cmaf_fragment(data, shift_ticks, sequence_number=physical_index)
        chunk = chunks[part]
        return Response(data[chunk.byte_start : chunk.byte_end], mimetype=mimetype)

    @app.get("/<rendition_name>/seg/<int:physical_index>.<int:part>.m4s", defaults=_SEG_DEFAULTS)
    @app.get("/<rendition_name>/cseg/<int:physical_index>.<int:part>.m4s", defaults={"mode": "global", "origin_loop": None})
    def video_part(rendition_name: str, physical_index: int, part: int, mode: str, origin_loop: int | None):
        return _serve_part(rendition_name, physical_index, part, mode, origin_loop)

    @app.get("/<rendition_name>/seg/<int:physical_index>.ts", defaults=_SEG_DEFAULTS)
    @app.get("/<rendition_name>/cseg/<int:physical_index>.ts", defaults={"mode": "global", "origin_loop": None})
    @app.get("/<rendition_name>/rseg/<int:origin_loop>/<int:physical_index>.ts", defaults={"mode": "origin"})
    def hls_ts_segment(rendition_name: str, physical_index: int, mode: str, origin_loop: int | None):
        if package.hls_format != "ts":
            abort(404)
        local_index, shift_ticks = _decode_segment_index(physical_index, mode, origin_loop)
        try:
            path = channel.hls_ts_segment_path(rendition_name, local_index)
        except (KeyError, IndexError):
            abort(404)
        return _serve_segment(path, "video/mp2t", ts=True, shift_ticks=shift_ticks)

    if package.has_audio:
        @app.get(f"/{LoopPackage.AUDIO_PLAYLIST}")
        def hls_audio_manifest():
            if package.hls_format == "ts" and package.hls_ts_mux_audio:
                abort(404)
            ch, window, query = _request_view()
            _block_for_reload(ch, window, None)
            body = ch.build_hls_audio_manifest(window=window, query=query)
            return _manifest_response(body, "application/vnd.apple.mpegurl", window)

        @app.get("/audio/init.mp4")
        def audio_init_segment():
            if package.audio_rendition.audio_sparse:
                abort(404)
            return _send_package_file(package.audio_rendition.audio_init_path())

        @app.get("/audio/init_<int:span>.mp4")
        def audio_span_init_segment(span: int):
            path = package.audio_rendition.audio_init_path_for_span(span) if package.audio_rendition.audio_sparse else None
            if path is None:
                abort(404)
            return _send_package_file(path)

        @app.get("/audio/seg/t/<int:span>/<int:time>.m4s", defaults={"mode": "local", "origin_loop": None})
        @app.get("/audio/cseg/t/<int:time>.m4s", defaults={"mode": "global", "origin_loop": None, "span": None})
        def audio_segment_by_time(time: int, span: int | None, mode: str, origin_loop: int | None):
            ticks = package.audio_rendition.audio_segment_boundary_ticks
            return audio_segment(_time_index(ticks, time, span), mode, origin_loop)

        @app.get("/audio/seg/<int:physical_index>.m4s", defaults=_SEG_DEFAULTS)
        @app.get("/audio/cseg/<int:physical_index>.m4s", defaults={"mode": "global", "origin_loop": None})
        def audio_segment(physical_index: int, mode: str, origin_loop: int | None):
            local_index, shift_ticks = _decode_segment_index(physical_index, mode, origin_loop)
            if shift_ticks is not None:
                # tfdt is in the audio track's own timescale, the shift in the package's.
                audio_ts = package.audio_rendition.audio_timescale(package.timescale)
                shift_ticks = round(shift_ticks * audio_ts / package.timescale)
            try:
                path = channel.audio_segment_bytes_path(local_index)
            except IndexError:
                abort(404)
            # sparse audio hole (path is None) -- _serve_segment 404s it.
            audio = package.audio_rendition
            pace = (
                _ll_pace(
                    audio.audio_segment_boundary_ticks, local_index, audio.audio_timescale(package.timescale),
                    mode, physical_index,
                )
                if channel.ll_capable
                else None
            )
            return _serve_segment(
                path, "audio/iso.segment", ts=False, shift_ticks=shift_ticks, sequence_number=physical_index,
                pace=pace,
            )

        @app.get("/audio/seg/<int:physical_index>.<int:part>.m4s", defaults=_SEG_DEFAULTS)
        @app.get("/audio/cseg/<int:physical_index>.<int:part>.m4s", defaults={"mode": "global", "origin_loop": None})
        def audio_part(physical_index: int, part: int, mode: str, origin_loop: int | None):
            return _serve_part(None, physical_index, part, mode, origin_loop)

        @app.get("/audio/seg/<int:physical_index>.ts", defaults=_SEG_DEFAULTS)
        @app.get("/audio/cseg/<int:physical_index>.ts", defaults={"mode": "global", "origin_loop": None})
        @app.get("/audio/rseg/<int:origin_loop>/<int:physical_index>.ts", defaults={"mode": "origin"})
        def hls_ts_audio_segment(physical_index: int, mode: str, origin_loop: int | None):
            if package.hls_format != "ts" or package.hls_ts_mux_audio:
                abort(404)
            local_index, shift_ticks = _decode_segment_index(physical_index, mode, origin_loop)
            if local_index >= package.segments_per_loop:
                abort(404)
            path = package.package_dir / "hls-ts" / "audio" / f"{local_index}.ts"
            if not path.is_file():  # sparse audio hole
                path = None
            return _serve_segment(path, "video/mp2t", ts=True, shift_ticks=shift_ticks)

    return app


def read_package_descriptor(package_dir: Path) -> dict:
    return json.loads((package_dir / "loop_descriptor.json").read_text())


def resolve_epoch_ticks(epoch_utc: str, timescale: int) -> int:
    """Convert an ISO8601 UTC epoch string to an integer tick count --
    callers should do this exactly once and feed the result into every
    subsequent request's arithmetic, never recompute it via accumulation."""
    epoch_dt = _dt.datetime.strptime(epoch_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=_dt.timezone.utc
    )
    return round(epoch_dt.timestamp() * timescale)


def resolve_window_segments(
    package_descriptor: dict,
    *,
    dvr_window_seconds: float = 30.0,
    window_segments: int | None = None,
) -> int:
    """Exact segment count if given, else derived from a DVR-window target
    using the package's nominal segment duration from bake time."""
    if window_segments is not None:
        return window_segments
    nominal_segment_duration_seconds = float(package_descriptor["segment_duration_seconds"])
    return max(1, math.ceil(dvr_window_seconds / nominal_segment_duration_seconds))


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve a loop package as live HLS/DASH")
    parser.add_argument("package_dir", type=Path, help="Path to a baked loop package directory")
    parser.add_argument(
        "--epoch-utc",
        type=str,
        required=True,
        help="Channel epoch, ISO8601 UTC (e.g. 2026-01-01T00:00:00Z). loop 0 starts here.",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--channel-name",
        default="",
        help="Channel name, shown in the manifests' header comments.",
    )
    parser.add_argument(
        "--dvr-window-seconds",
        type=float,
        default=30.0,
        help="Approximate size of the DVR window / sliding manifest, in "
        "seconds (default: 30). Converted to a segment count using the "
        "package's nominal --segment-duration from bake time. Ignored if "
        "--window-segments is also given.",
    )
    parser.add_argument(
        "--window-segments",
        type=int,
        default=None,
        help="Exact number of segments to advertise per manifest response "
        "(HLS sliding window / DASH SegmentTimeline). Overrides "
        "--dvr-window-seconds if given.",
    )
    parser.add_argument(
        "--continuous-timeline",
        action="store_true",
        help="SCOPE.md §12: rewrite every segment's internal PTS/DTS/PCR "
        "(TS) or tfdt (CMAF) per request so the channel is one genuinely "
        "continuous timeline with no #EXT-X-DISCONTINUITY / DASH Period "
        "restart at the loop wrap. Requires a package with no internal "
        "asset-boundary discontinuities and 64-bit (v1) tfdt CMAF "
        "fragments -- checked once at startup, hard-fails otherwise.",
    )
    parser.add_argument(
        "--period-on-segmentation",
        default="",
        metavar="IDS",
        help="Comma-separated segmentation_type_id values (e.g. "
        "0x22,0x23,0x30,0x31) whose markers force a new DASH Period / "
        "#EXT-X-DISCONTINUITY at the segment they fall in, in either "
        "timeline mode. Signal only: with --continuous-timeline the "
        "timestamps stay continuous across the new Period. Channel-level; "
        "SCOPE.md §14.",
    )
    parser.add_argument(
        "--dash-addressing",
        choices=DASH_ADDRESSING_CHOICES,
        default="number",
        help="DASH SegmentTemplate addressing: number ($Number$ + startNumber, "
        "default) or time ($Time$ = each <S t=...>). HLS is unaffected.",
    )
    parser.add_argument(
        "--dash-timeline",
        choices=DASH_TIMELINE_CHOICES,
        default="full",
        help="DASH SegmentTimeline style: full (every <S> has @t and @d, a "
        "SegmentTemplate per representation; default) or compact (implicit @t "
        "where contiguous, @r for runs of equal @d, one shared SegmentTemplate "
        "when the video renditions match). HLS is unaffected.",
    )
    parser.add_argument(
        "--ll-dash",
        action="store_true",
        help="Low-latency DASH: advertise availabilityTimeOffset/ServiceDescription in the "
        "MPD and stream the segment in progress chunk by chunk, each chunk released at its "
        "own wall-clock end. Needs a package baked with `bake.py --chunk-duration`, and a "
        "threaded/async server (held responses) -- not the default sync gunicorn worker.",
    )
    parser.add_argument(
        "--ll-hls",
        action="store_true",
        help="Low-latency HLS (Apple LL-HLS): HLS media playlists advertise parts, a preload hint, rendition reports "
        "and blocking reload; parts are the baked chunks, served at .../seg/<n>.<k>.m4s and held until due. Needs a "
        "package baked with `bake.py --chunk-duration` and --hls-format cmaf, and a threaded/async server.",
    )
    parser.add_argument(
        "--ll-optin",
        action="store_true",
        help="Low-latency DASH delivery is available (as with --ll-dash) but an MPD only advertises it "
        "when asked with ?low_latency=true (needs --query-overrides). Implied by --ll-dash, which advertises by default.",
    )
    parser.add_argument(
        "--ll-target-latency",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Latency the low-latency MPD asks players to hold (default: 4 x chunk, at least 1 s).",
    )
    parser.add_argument(
        "--ll-utc-timing",
        choices=LL_UTC_TIMING_CHOICES,
        default="direct",
        help="How the low-latency MPD lets players sync their clock: direct (default: the time "
        "written into the MPD, which is then served no-store -- accurate only while the MPD is "
        "not cached), http (a polled /time endpoint; needs --ll-utc-timing-url except on localhost) "
        "or none (the player's own clock)."
    )
    parser.add_argument(
        "--ll-utc-timing-url",
        default="",
        metavar="URL",
        help="Absolute URL of this server's /time, for --ll-utc-timing http. Without it the URL is derived "
        "from the request only when it is addressed to localhost; for any other Host no UTCTiming is "
        "written (a Host header is client-controlled, and a shared cache could keep a forged one).",
    )
    parser.add_argument(
        "--timeshift",
        action="store_true",
        help="SCOPE.md §13: honour the startover/catchup query parameters on the "
        "normal manifest URLs (start, end, full_loops, offset).",
    )
    parser.add_argument(
        "--query-overrides",
        action="store_true",
        help="SCOPE.md §13.8: honour the per-request override parameters "
        "(timeline_mode, epoch, dvr_window, split_on, cue_tags, ...); without it "
        "they are ignored and the channel's own settings always apply.",
    )
    parser.add_argument(
        "--query-prefix",
        default="",
        metavar="PREFIX",
        help="Prefix for every query parameter name (e.g. 'dsl_' -> dsl_start, "
        "dsl_offset), so a caller that uses some of these names itself cannot clash.",
    )
    parser.add_argument("--timeshift-start-param", default="start")
    parser.add_argument("--timeshift-end-param", default="end")
    parser.add_argument("--timeshift-max-span-seconds", type=int, default=21600)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    package_descriptor = read_package_descriptor(args.package_dir)
    timescale = int(package_descriptor["timescale"])
    epoch_ticks = resolve_epoch_ticks(args.epoch_utc, timescale)
    window_segments = resolve_window_segments(
        package_descriptor,
        dvr_window_seconds=args.dvr_window_seconds,
        window_segments=args.window_segments,
    )

    logger.info(
        "DVR window: %d segment(s) (~%.1fs nominal, requested %.1fs)",
        window_segments,
        window_segments * float(package_descriptor["segment_duration_seconds"]),
        args.dvr_window_seconds,
    )

    logger.warning(
        "Running Flask's built-in dev server -- single-threaded, serializes "
        "concurrent requests. Fine for local dev/testing and local-docker; "
        "the ecs-express Docker image's production path fronts this app "
        "with gunicorn instead (see wsgi.py, docker-entrypoint.sh, PERFS.md)."
    )
    app = create_app(
        args.package_dir,
        epoch_ticks,
        window_segments=window_segments,
        continuous=args.continuous_timeline,
        period_on_segmentation=parse_segmentation_type_ids(args.period_on_segmentation),
        channel_name=args.channel_name,
        dash_addressing=args.dash_addressing,
        dash_timeline=args.dash_timeline,
        ll_dash=args.ll_dash,
        ll_target_latency=args.ll_target_latency,
        ll_utc_timing_url=args.ll_utc_timing_url,
        ll_utc_timing=args.ll_utc_timing,
        ll_optin=args.ll_optin,
        ll_hls=args.ll_hls,
        timeshift=TimeshiftConfig(
            timeshift=args.timeshift,
            overrides=args.query_overrides,
            start_param=args.timeshift_start_param,
            end_param=args.timeshift_end_param,
            max_span_seconds=args.timeshift_max_span_seconds,
            prefix=args.query_prefix,
        ),
    )
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
