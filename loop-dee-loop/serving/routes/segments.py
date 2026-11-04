"""Segment and init-segment bytes (video, audio, MPEG-TS) and the $Time$ URL forms."""

from __future__ import annotations

from flask import abort, Flask

from serving.httputil import _send_package_file
from timeshift import TimeshiftError

from serving.context import SEG_DEFAULTS, ServeContext


def register(app: Flask, ctx: ServeContext) -> None:
    """Segment and init-segment bytes (video, audio, MPEG-TS) and the $Time$ URL forms."""
    _decode_segment_index = ctx.decode_segment_index
    _ll_pace = ctx.ll_pace
    _serve_segment = ctx.serve_segment
    _video_timescale = ctx.package.video_track_timescale
    channel = ctx.channel
    package = ctx.package

    _SEG_DEFAULTS = SEG_DEFAULTS

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

