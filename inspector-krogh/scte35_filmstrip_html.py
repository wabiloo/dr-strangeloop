"""Horizontal filmstrip view of a krogh report.

Shows every extracted frame in one time-ordered strip, with marker spans
drawn as stacked bars above it (one row per nesting depth), an ellipsis
column wherever the extracted frames aren't contiguous, and a timescale row
below.

Pure function of the same report dict scte35_report_html.render_html()
consumes -- no ffmpeg/tsduck involved, so it can run on any scte-report.json.
"""

from __future__ import annotations

import base64
from html import escape
from pathlib import Path
from typing import Optional

from krogh_common import HEADER_CSS, META_CSS, header_id_html, meta_table_html, ordered_type_codes, type_colors

COL_W_FRAME = 68
COL_W_ELLIPSIS = 52
THUMB_H = 40


def _fmt_time(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    return f"{h:02d}:{m:02d}:{rem // 1000:02d}.{rem % 1000:03d}"


def _img_data_uri(rel_path: Optional[str], base_dir: Optional[Path]) -> Optional[str]:
    if not rel_path or base_dir is None:
        return None
    full = base_dir / rel_path
    if full.is_file():
        return f"data:image/jpeg;base64,{base64.b64encode(full.read_bytes()).decode()}"
    return None


def _build_columns(report: dict, frame_dur: float, base_dir: Optional[Path]) -> list[dict]:
    """Merge every extracted shot into one time-ordered column list,
    quantized to the frame grid so boundaries landing on the same frame share
    a column, then splice in an ellipsis wherever neighbours aren't adjacent."""
    by_pts: dict[int, dict] = {}

    for marker in report["markers"]:
        for boundary in ("start", "stop"):
            if marker.get(boundary) is None:
                continue
            key = f"evt{marker['event_id']}_{boundary}"
            for shot in report.get("frames", {}).get(key, []):
                idx = round(shot["pts_seconds"] / frame_dur)
                col = by_pts.setdefault(idx, {
                    "type": "frame", "pts": shot["pts_seconds"],
                    "is_idr": shot.get("is_idr", False),
                    "img": _img_data_uri(shot.get("file"), base_dir),
                    "tags": [], "landing": [],
                })
                col["is_idr"] = col["is_idr"] or shot.get("is_idr", False)
                if col["img"] is None:
                    col["img"] = _img_data_uri(shot.get("file"), base_dir)
                if shot["label"] == "+0":
                    col["landing"].append({
                        "event_id": marker["event_id"], "boundary": boundary,
                        "type_code": marker["type_code"], "type_name": marker["type_name"],
                    })

    for key in ("asset_start", "asset_end"):
        for shot in report.get("frames", {}).get(key, []):
            idx = round(shot["pts_seconds"] / frame_dur)
            col = by_pts.setdefault(idx, {
                "type": "frame", "pts": shot["pts_seconds"],
                "is_idr": shot.get("is_idr", False),
                "img": _img_data_uri(shot.get("file"), base_dir),
                "tags": [], "landing": [],
            })
            col["is_idr"] = col["is_idr"] or shot.get("is_idr", False)

    ordered = [by_pts[k] for k in sorted(by_pts)]
    if not ordered:
        return []

    columns: list[dict] = [ordered[0]]
    prev_idx = round(ordered[0]["pts"] / frame_dur)
    for col in ordered[1:]:
        idx = round(col["pts"] / frame_dur)
        if idx - prev_idx > 1:
            columns.append({"type": "ellipsis", "gap_seconds": (idx - prev_idx - 1) * frame_dur})
        columns.append(col)
        prev_idx = idx
    return columns


def _span_bars(report: dict, columns: list[dict], frame_dur: float) -> tuple[list[dict], list[dict]]:
    """Locate each marker's start/stop column so its bar gets exact grid
    bounds. Spans and instants (pins) are returned separately."""
    pos_by_idx: dict[int, int] = {}
    for col_i, col in enumerate(columns):
        if col["type"] == "frame":
            pos_by_idx[round(col["pts"] / frame_dur)] = col_i

    spans, pins = [], []
    for marker in report["markers"]:
        start = marker.get("start")
        stop = marker.get("stop")
        if marker["is_instant"] and start is not None:
            col_i = pos_by_idx.get(round(start["pts_seconds"] / frame_dur))
            if col_i is not None:
                pins.append({"marker": marker, "col": col_i})
            continue
        if start is None or stop is None:
            continue
        c0 = pos_by_idx.get(round(start["pts_seconds"] / frame_dur))
        c1 = pos_by_idx.get(round(stop["pts_seconds"] / frame_dur))
        if c0 is None or c1 is None:
            continue
        spans.append({"marker": marker, "col_start": c0, "col_end": c1})
    return spans, pins


def _upid_short(ev: Optional[dict]) -> str:
    if not ev or not ev.get("upid_hex"):
        return ""
    h = ev["upid_hex"].replace(" ", "")
    return h if len(h) <= 18 else h[:16] + "…"


def _bar_label_lines(marker: dict) -> tuple[str, str]:
    line1 = f'{marker["type_name"]} #{marker["event_id"]}'
    seg = marker.get("start") or marker.get("stop") or {}
    parts = []
    if seg.get("segment_num") is not None:
        parts.append(f'seg {seg["segment_num"]}/{seg.get("segments_expected") or 0}')
    upid = _upid_short(seg)
    if upid:
        parts.append(f'upid {upid}')
    return line1, " · ".join(parts)


def _place_tags(columns: list[dict]) -> None:
    """Start tags sit on the frame the marker lands on (left-aligned); stop
    tags sit on the frame *before* it (right-aligned), since a span ends one
    frame ahead of its stop marker."""
    for i, col in enumerate(columns):
        if col["type"] != "frame":
            continue
        for t in col["landing"]:
            if t["boundary"] == "start":
                col["tags"].append({**t, "side": "start"})
            else:
                j = i - 1 if i > 0 and columns[i - 1]["type"] == "frame" else i
                columns[j]["tags"].append({**t, "side": "stop"})


def _assign_rows(spans: list[dict]) -> tuple[list[str], int]:
    """One row per marker type (BRK/PPO/PAD first); overlapping spans of the
    same type get extra lanes. Sets span['row'] (0-based); returns
    (ordered type codes, total row count)."""
    by_type: dict[str, list[dict]] = {}
    for sp in spans:
        by_type.setdefault(sp["marker"]["type_code"], []).append(sp)
    codes = ordered_type_codes(by_type)
    row = 0
    for code in codes:
        lane_ends: list[int] = []
        for sp in sorted(by_type[code], key=lambda x: x["col_start"]):
            end = max(sp["col_end"], sp["col_start"] + 1)
            for lane, last in enumerate(lane_ends):
                if last <= sp["col_start"]:
                    lane_ends[lane] = end
                    break
            else:
                lane = len(lane_ends)
                lane_ends.append(end)
            sp["row"] = row + lane
            sp["end_col"] = end
        row += len(lane_ends)
    return codes, row


def render_filmstrip(report: dict, base_dir: Optional[Path] = None) -> str:
    fps = report["source"].get("fps") or 25.0
    frame_dur = 1.0 / fps

    columns = _build_columns(report, frame_dur, base_dir)
    _place_tags(columns)
    spans, pins = _span_bars(report, columns, frame_dur)
    type_codes, span_rows = _assign_rows(spans)

    colors = type_colors(type_codes)

    n_cols = len(columns)
    col_widths = [COL_W_ELLIPSIS if c["type"] == "ellipsis" else COL_W_FRAME for c in columns]
    grid_template_columns = " ".join(f"{w}px" for w in col_widths)

    max_tags = max((len(c["tags"]) for c in columns if c["type"] == "frame"), default=0)
    frame_h = THUMB_H + 8 + 13 * max(1, max_tags)

    pin_row = span_rows + 1 if pins else 0
    frame_row = span_rows + (1 if pins else 0) + 1
    time_row = frame_row + 1

    row_heights = ["26px"] * span_rows + (["20px"] if pins else []) + [f"{frame_h}px", "22px"]
    grid_template_rows = " ".join(row_heights)

    cells: list[str] = []

    for sp in spans:
        m = sp["marker"]
        bg, border, fg = colors[m["type_code"]]
        line1, line2 = _bar_label_lines(m)
        # Bar covers [start frame, stop frame): the stop marker's landing
        # frame already belongs to the next segment, and this keeps
        # back-to-back same-type spans from overlapping at the handoff.
        gcol = f"{sp['col_start'] + 1} / {sp['end_col'] + 1}"
        cells.append(f"""
        <div class="span-bar" style="grid-column:{gcol};grid-row:{sp['row'] + 1};background:{bg};border-color:{border};color:{fg}">
          <span class="sb-l1">{escape(line1)}</span>{f'<span class="sb-l2">{escape(line2)}</span>' if line2 else ''}
        </div>""")

    for p in pins:
        m = p["marker"]
        line1, line2 = _bar_label_lines(m)
        title = escape(line1 + (" · " + line2 if line2 else ""))
        cells.append(f"""
        <div class="pin" style="grid-column:{p['col'] + 1};grid-row:{pin_row}" title="{title}">
          <span class="pin-dot"></span><span class="pin-label">{escape(m['type_code'])} #{m['event_id']}</span>
        </div>""")

    for i, col in enumerate(columns):
        gcol = i + 1
        if col["type"] == "ellipsis":
            cells.append(f"""
        <div class="cell ellipsis" style="grid-column:{gcol};grid-row:{frame_row}">
          <span class="ell-dots">⋯</span><span class="ell-gap">+{col['gap_seconds']:.1f}s</span>
        </div>""")
            continue

        tags = "".join(
            f'<span class="land-tag {t["side"]}" title="{escape(t["type_name"])} #{t["event_id"]} {t["side"]}">{escape(t["type_code"])}</span>'
            for t in col["tags"]
        )
        img = f'<img src="{col["img"]}" alt="frame">' if col["img"] else '<div class="no-img"></div>'
        idr_cls = "idr" if col["is_idr"] else ""
        cells.append(f"""
        <div class="cell frame" style="grid-column:{gcol};grid-row:{frame_row}">
          <div class="thumb {idr_cls}">{img}</div>
          <div class="land-tags">{tags}</div>
        </div>
        <div class="cell time" style="grid-column:{gcol};grid-row:{time_row}">{_fmt_time(col['pts'])}</div>""")

    grid_html = "".join(cells)
    src = report["source"]
    name = escape(src.get("name", ""))

    meta_html = meta_table_html([
        ("Video", f"{src.get('codec', '?')} {src.get('width')}×{src.get('height')} @ {fps:g} fps"),
        ("Duration", _fmt_time(src.get("duration") or 0)),
        ("SCTE-35 markers", str(report["summary"]["marker_count"])),
        ("Frames shown", str(sum(1 for c in columns if c["type"] == "frame"))),
    ])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inspector Krogh — {name}</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0c0c14;color:#c4c4d4;font-family:"JetBrains Mono","Fira Code","SF Mono",monospace;font-size:12px;padding:24px}}
header{{display:flex;flex-wrap:wrap;align-items:flex-end;gap:16px;border-bottom:1px solid #252536;padding-bottom:14px;margin-bottom:18px}}
.legend{{display:flex;gap:16px;margin-bottom:14px;font-size:11px;color:#b0b0cc;flex-wrap:wrap;align-items:center}}
.legend .sw{{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px;vertical-align:middle}}
.legend .sw.idr{{background:transparent;border:2px solid #ff3b3b;width:12px;height:9px}}

.filmstrip-scroll{{overflow-x:auto;border:1px solid #232336;border-radius:6px;background:#0a0a12;padding:14px 14px 10px}}
.filmstrip-grid{{display:grid;grid-template-columns:{grid_template_columns};grid-template-rows:{grid_template_rows};column-gap:2px;row-gap:3px;align-items:stretch}}

.span-bar{{border:1px solid;border-radius:4px;display:flex;flex-direction:column;justify-content:center;padding:2px 8px;overflow:hidden;white-space:nowrap;font-size:10px}}
.sb-l1{{font-weight:700;text-overflow:ellipsis;overflow:hidden}}
.sb-l2{{opacity:.75;font-size:9px;text-overflow:ellipsis;overflow:hidden}}

.pin{{display:flex;align-items:center;gap:4px;font-size:9px;color:#ffbb55;white-space:nowrap}}
.pin-dot{{width:6px;height:6px;border-radius:50%;background:#ffbb55;flex-shrink:0}}

.cell.frame{{display:flex;flex-direction:column;align-items:center;justify-content:flex-start;gap:2px;padding:0 2px}}
.thumb{{width:100%;height:{THUMB_H}px;border-radius:3px;overflow:hidden;border:2px solid transparent;background:#050508}}
.thumb.idr{{border-color:#ff3b3b}}
.thumb img{{width:100%;height:100%;object-fit:cover;display:block;opacity:.9}}
.no-img{{width:100%;height:100%;background:#111}}
.land-tags{{display:flex;flex-direction:column;width:100%;gap:1px}}
.land-tag{{font-size:9px;font-weight:700;padding:0 3px;border-radius:2px;line-height:1.5}}
.land-tag.start{{align-self:flex-start;background:#0b3318;color:#66ffaa}}
.land-tag.stop{{align-self:flex-end;background:#3a1810;color:#ff9955}}

.cell.ellipsis{{display:flex;flex-direction:column;align-items:center;justify-content:center;color:#8f8fae}}
.ell-dots{{font-size:16px;line-height:1}}
.ell-gap{{font-size:9px;margin-top:2px}}

.cell.time{{font-size:9px;color:#a0a0be;text-align:left;padding-left:2px;padding-top:2px;border-top:1px solid #1a1a28;white-space:nowrap;overflow:hidden;letter-spacing:-.3px}}
{HEADER_CSS}{META_CSS}
</style>
</head>
<body>
<header>
  {header_id_html("SCTE-35 marker filmstrip — markers found in the transport stream, with frames at each boundary", src.get("name", ""))}
  {meta_html}
</header>

<div class="legend">
  <span><span class="sw idr"></span>IDR frame</span>
  <span><span class="land-tag start" style="padding:1px 4px">TYPE</span>marker starts on this frame</span>
  <span><span class="land-tag stop" style="padding:1px 4px">TYPE</span>marker ends after this frame</span>
</div>

<div class="filmstrip-scroll">
  <div class="filmstrip-grid">
    {grid_html}
  </div>
</div>
</body>
</html>"""
