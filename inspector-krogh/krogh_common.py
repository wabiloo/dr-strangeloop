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
        "<h1>🕵️ Inspector Krogh</h1>"
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


META_CSS = """
table.meta-table{border-collapse:collapse;font-size:12px;margin-left:auto}
.hdr-status-badge{margin-top:12px}
.meta-table th{color:#9a9ab8;font-weight:600;text-align:left;padding:2px 16px 2px 0;white-space:nowrap}
.meta-table td{color:#e0e0f0;padding:2px 0}
.meta-table td.warn{color:#ffcc66;position:relative}
.meta-table td.warn[data-tip]{cursor:help}
.meta-table td.warn[data-tip]:hover::after{content:attr(data-tip);position:absolute;right:0;top:100%;z-index:20;width:max-content;max-width:340px;padding:7px 10px;margin-top:4px;background:#1d1d33;border:1px solid #ffcc66;border-radius:5px;color:#e8e8f4;font-weight:400;white-space:normal;box-shadow:0 6px 20px rgba(0,0,0,.6)}
.meta-table td.warn::before{content:"\\26A0\\FE0E";font-weight:700;margin-right:7px}
"""


def meta_table_html(rows: list[tuple]) -> str:
    """Labelled key/value table for the page header. Rows are
    (label, value), (label, value, "warn") or (label, value, "warn", hint);
    a hint shows in a tooltip on hover. All text is HTML-escaped."""
    out = []
    for row in rows:
        attrs = ""
        if len(row) > 2 and row[2] == "warn":
            attrs = ' class="warn"'
            if len(row) > 3 and row[3]:
                attrs += f' data-tip="{escape(row[3], quote=True)}"'
        out.append(f"<tr><th>{escape(row[0])}</th><td{attrs}>{escape(row[1])}</td></tr>")
    return f'<table class="meta-table">{"".join(out)}</table>'
