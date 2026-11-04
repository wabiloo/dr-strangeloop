"""HLS and DASH manifests: the multivariant playlist, the media playlists and the MPD."""

from __future__ import annotations

from flask import Flask, abort, request

from serving.context import ServeContext
from serving.httputil import _is_loopback_host
from serving.package import LoopPackage


def register(app: Flask, ctx: ServeContext) -> None:
    """HLS and DASH manifests."""
    _block_for_reload = ctx.block_for_reload
    _manifest_response = ctx.manifest_response
    _request_view = ctx.request_view
    package = ctx.package

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

    if package.has_audio:
        @app.get(f"/{LoopPackage.AUDIO_PLAYLIST}")
        def hls_audio_manifest():
            if package.hls_format == "ts" and package.hls_ts_mux_audio:
                abort(404)
            ch, window, query = _request_view()
            _block_for_reload(ch, window, None)
            body = ch.build_hls_audio_manifest(window=window, query=query)
            return _manifest_response(body, "application/vnd.apple.mpegurl", window)

