"""Shared page-header markup so every Inspector Krogh report looks related."""

from __future__ import annotations

from html import escape

HEADER_CSS = """
header .ik-id h1,#hdr .ik-id h1{font-size:20px;font-weight:700;color:#e8e8f4;letter-spacing:-.5px;white-space:normal;overflow:visible}
.ik-sub{font-size:11px;color:#7a7a98;margin-top:2px}
.ik-file{font-size:13px;color:#c4c4d4;font-weight:600;margin-top:6px;word-break:break-all}
"""


def header_id_html(subtitle: str, filename: str) -> str:
    return (
        '<div class="ik-id">'
        "<h1>Inspector Krogh</h1>"
        f'<div class="ik-sub">{escape(subtitle)}</div>'
        f'<div class="ik-file">{escape(filename)}</div>'
        "</div>"
    )
