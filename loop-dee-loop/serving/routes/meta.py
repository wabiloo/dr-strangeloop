"""Service endpoints: health, the window JSON, the clock, the OpenAPI spec and docs."""

from __future__ import annotations

from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file
from timeshift import FULL_LOOPS_PARAM, OFFSET_PARAM, TIMELINE_PARAM

from serving.constants import _DOCS_HTML
from serving.context import ServeContext


def register(app: Flask, ctx: ServeContext) -> None:
    """Service endpoints: health, the window JSON, the clock, the OpenAPI spec and docs."""
    _request_view = ctx.request_view
    channel = ctx.channel
    epoch_ticks = ctx.epoch_ticks
    package = ctx.package
    package_dir = ctx.package_dir
    process_start_ticks = ctx.process_start_ticks
    ts_cfg = ctx.ts_cfg

    @app.after_request
    def _add_cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

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
        return send_file(Path(__file__).resolve().parents[2] / "openapi.yaml", mimetype="application/yaml")

    @app.get("/docs")
    def api_docs():
        # Relative spec-url so it also resolves behind a path-prefixing proxy (igor).
        return Response(_DOCS_HTML, mimetype="text/html")

    @app.get("/time")
    def server_time():
        """The channel clock as an xs:dateTime, for the low-latency MPD's
        `UTCTiming` (`urn:mpeg:dash:utc:http-xsdate:2014`). Never cached."""
        resp = Response(channel._iso_ticks(channel.now_ticks()), mimetype="text/plain")
        resp.headers["Cache-Control"] = "no-store"
        return resp

