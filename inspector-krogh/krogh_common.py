"""Shared page-header markup so every Inspector Krogh report looks related."""

from __future__ import annotations

from html import escape

HEADER_CSS = """
header .ik-id h1,#hdr .ik-id h1{font-size:20px;font-weight:700;color:#e8e8f4;letter-spacing:-.5px;white-space:normal;overflow:visible}
.ik-sub{font-size:11px;color:#b0b0cc;margin-top:2px}
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


TYPE_ORDER = ["BRK", "PPO", "PAD"]
_TYPE_COLORS = {
    "BRK": ("#1c3f66", "#2a5788", "#8fc4ff"),
    "PPO": ("#3a2a5c", "#5c4088", "#c9a8ff"),
    "PAD": ("#5c2a3a", "#884058", "#ffa8c4"),
}
_FALLBACK_COLORS = [
    ("#2a5c3a", "#408858", "#a8ffc4"),
    ("#5c4a1c", "#88702a", "#ffdf8f"),
    ("#1c5c5c", "#2a8888", "#8ffff0"),
]


def ordered_type_codes(codes) -> list[str]:
    """BRK/PPO/PAD first, then any other marker types alphabetically."""
    present = set(codes)
    return [c for c in TYPE_ORDER if c in present] + sorted(present - set(TYPE_ORDER))


def type_colors(codes) -> dict[str, tuple[str, str, str]]:
    """(background, border, text) per marker type code, stable across reports."""
    out: dict[str, tuple[str, str, str]] = {}
    for code in ordered_type_codes(codes):
        out[code] = _TYPE_COLORS.get(code) or _FALLBACK_COLORS[len(out) % len(_FALLBACK_COLORS)]
    return out
