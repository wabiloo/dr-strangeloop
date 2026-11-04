"""Low-latency HLS parts: the video and audio part URLs (held until due)."""

from __future__ import annotations

from flask import Flask

from serving.context import SEG_DEFAULTS, ServeContext


def register(app: Flask, ctx: ServeContext) -> None:
    """Low-latency HLS parts: the video and audio part URLs (held until due)."""
    _serve_part = ctx.serve_part
    package = ctx.package

    _SEG_DEFAULTS = SEG_DEFAULTS

    @app.get("/<rendition_name>/seg/<int:physical_index>.<int:part>.m4s", defaults=_SEG_DEFAULTS)
    @app.get("/<rendition_name>/cseg/<int:physical_index>.<int:part>.m4s", defaults={"mode": "global", "origin_loop": None})
    def video_part(rendition_name: str, physical_index: int, part: int, mode: str, origin_loop: int | None):
        return _serve_part(rendition_name, physical_index, part, mode, origin_loop)

    if package.has_audio:
        @app.get("/audio/seg/<int:physical_index>.<int:part>.m4s", defaults=_SEG_DEFAULTS)
        @app.get("/audio/cseg/<int:physical_index>.<int:part>.m4s", defaults={"mode": "global", "origin_loop": None})
        def audio_part(physical_index: int, part: int, mode: str, origin_loop: int | None):
            return _serve_part(None, physical_index, part, mode, origin_loop)

