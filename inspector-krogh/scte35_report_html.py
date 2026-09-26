"""Pure JSON -> HTML renderer for krogh reports.

Deliberately has no ffmpeg/tsduck/subprocess dependency: it only reads the
dict produced by `krogh.build_report()` (or the `scte-report.json`
serialization of it) plus whatever frame JPEGs it already points at on
disk. That split is what lets a consumer like igor re-render the HTML
(e.g. after a template change) without re-scanning the `.ts`, and lets the
same function be reused unchanged whether it's called from the CLI
(`krogh --render-only`) or imported directly.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

from html import escape

from krogh_common import HEADER_CSS, META_CSS, header_id_html, meta_table_html, ordered_type_codes, type_colors


def _fmt_time(seconds: float) -> str:
    if seconds is None:
        return "—"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def _img_data_uri(rel_path: Optional[str], base_dir: Optional[Path]) -> Optional[str]:
    """Inline frames as base64 data URIs when the on-disk file is reachable,
    so the HTML stays a single, fully self-contained, shareable file (same
    convention franken-ts's own report.py uses) -- falls back to a relative
    <img src> (still works when opened next to its frames/ dir) if the file
    can't be read from here (e.g. rendering from a JSON that traveled
    without its sibling frames/)."""
    if not rel_path:
        return None
    if base_dir is not None:
        full = base_dir / rel_path
        if full.is_file():
            b64 = base64.b64encode(full.read_bytes()).decode()
            return f"data:image/jpeg;base64,{b64}"
    return rel_path


def _frame_tag(shot: dict, base_dir: Optional[Path], boundary: str) -> str:
    label = shot["label"]
    src = _img_data_uri(shot.get("file"), base_dir)
    idr_badge = '<span class="idr-badge">IDR</span>' if shot.get("is_idr") else ""
    classes = ["frame"]
    if label == "+0":
        classes.append(f"mark-{boundary}")
    if shot.get("is_idr"):
        classes.append("idr")
    if src:
        img = f'<img src="{src}" alt="frame {label}">'
    else:
        img = '<div class="no-frame">no frame</div>'
    return (
        f'<div class="{" ".join(classes)}">{img}'
        f'<div class="frame-label">{label}{idr_badge}</div></div>'
    )


def _checks_for(checks: list[dict], event_id: int) -> list[dict]:
    return [c for c in checks if c["event_id"] == event_id]


def _check_event_label(c: dict) -> str:
    return f"#{c['event_id']}" if c.get("event_id") is not None else f"join {c.get('transition')}"


def _check_badge(checks: list[dict]) -> str:
    if not checks:
        return ""
    failed = [c for c in checks if not c["pass"]]
    if failed:
        return f'<span class="badge bad">{len(failed)} check(s) failed</span>'
    return '<span class="badge ok">all checks passed</span>'


def _boundary_block(marker: dict, boundary: str, frames: dict, m_checks: list[dict], base_dir: Optional[Path]) -> str:
    ev = marker[boundary]
    shots = frames.get(f"evt{marker['event_id']}_{boundary}", [])
    frame_html = (
        "".join(_frame_tag(s, base_dir, boundary) for s in shots)
        if shots else '<div class="no-frames-note">frames not extracted</div>'
    )
    checks_html = "".join(
        f'<div class="check {"pass" if c["pass"] else "fail"}">'
        f'<span class="check-name">{c["check"]}</span>'
        f'<span class="check-detail">{c["detail"]}</span></div>'
        for c in m_checks if c["boundary"] == boundary
    )
    return f"""
        <div class="boundary {boundary}">
          <div class="bh">
            <span class="tag {boundary}">{boundary.upper()}</span>
            <span class="ts">{_fmt_time(ev["pts_seconds"])}</span>
            <span class="pts">PTS {ev["pts_ticks"]:,}</span>
          </div>
          <div class="frames">{frame_html}</div>
          {_upid_panel(ev)}
          {f'<div class="checks">{checks_html}</div>' if checks_html else ""}
        </div>"""


def _marker_card(marker: dict, frames: dict, checks: list[dict], base_dir: Optional[Path], colors: dict) -> str:
    eid = marker["event_id"]
    m_checks = _checks_for(checks, eid)
    dur = marker.get("duration_seconds")
    dur_str = f'<span class="dur">{dur:g}s · {_fmt_time(dur)}</span>' if dur is not None else ""

    blocks = [
        _boundary_block(marker, boundary, frames, m_checks, base_dir)
        for boundary in ("start", "stop") if marker.get(boundary) is not None
    ]
    if len(blocks) == 2:
        gap = f'<div class="span-gap"><span class="dots">⋯</span><span class="gap-dur">{_fmt_time(dur)}</span></div>' if dur is not None else '<div class="span-gap"><span class="dots">⋯</span></div>'
        body = blocks[0] + gap + blocks[1]
    else:
        body = "".join(blocks)

    assets_str = (
        '<span class="assets">' + "".join(f'<span class="asset-chip">{escape(a)}</span>' for a in marker["assets"]) + "</span>"
        if marker.get("assets") else ""
    )
    type_badge = f'<span class="type-badge">{marker["type_code"]}</span>'
    splice_badge = f'<span class="splice-badge {marker["splice_type"]}">{marker["splice_type"]}</span>'

    bg, border, fg = colors[marker["type_code"]]
    return f"""
    <div class="marker-card" id="marker-{eid}" style="--t-bg:{bg};--t-border:{border};--t-fg:{fg}">
      <div class="marker-hdr">
        <span class="mid">Event #{eid}</span>
        {type_badge}{splice_badge}
        <span class="mname">{marker["type_name"]}</span>
        {dur_str}
        {assets_str}
        <span class="hdr-status">{_check_badge(m_checks)}</span>
      </div>
      <div class="span-row">{body}</div>
    </div>"""


def _marker_time(marker: dict) -> float:
    ev = marker.get("start") or marker.get("stop")
    return ev["pts_seconds"]


def _transition_card(tr: dict, frames: dict, checks: list[dict], base_dir: Optional[Path]) -> str:
    shots = frames.get(f"trans{tr['index']}", [])
    frame_html = (
        "".join(_frame_tag(s, base_dir, "join") for s in shots)
        if shots else '<div class="no-frames-note">frames not extracted</div>'
    )
    t_checks = [c for c in checks if c.get("transition") == tr["index"]]
    checks_html = "".join(
        f'<div class="check {"pass" if c["pass"] else "fail"}">'
        f'<span class="check-name">{c["check"]}</span>'
        f'<span class="check-detail">{c["detail"]}</span></div>'
        for c in t_checks
    )
    from_a, to_a = escape(str(tr["from_asset"] or tr["from_file"])), escape(str(tr["to_asset"] or tr["to_file"]))
    return f"""
    <div class="marker-card transition" id="transition-{tr['index']}" style="--t-bg:#26263a;--t-border:#6a6a8a;--t-fg:#d4d4e4">
      <div class="marker-hdr">
        <span class="mid">Asset boundary</span>
        <span class="type-badge">JOIN</span>
        <span class="mname">{from_a} → {to_a}</span>
        <span class="dur">no SCTE-35 marker</span>
        <span class="hdr-status">{_check_badge(t_checks)}</span>
      </div>
      <div class="span-row">
        <div class="boundary join">
          <div class="bh">
            <span class="tag join">JOIN</span>
            <span class="ts">{_fmt_time(tr["time"])}</span>
            <span class="pts">{escape(str(tr["from_file"]))} → {escape(str(tr["to_file"]))}</span>
          </div>
          <div class="frames">{frame_html}</div>
          {f'<div class="checks">{checks_html}</div>' if checks_html else ""}
        </div>
      </div>
    </div>"""


def _grouped_cards(markers: list[dict], transitions: list[dict], frames: dict, checks: list[dict],
                   base_dir: Optional[Path], colors: dict) -> tuple[str, set[int]]:
    """Cards grouped by the timepoint they start (or occur) at, in time order;
    within a group BRK/PPO/PAD come first, then other types, then event id.
    Asset joins without a marker sit in their own group at their time.
    Returns (html, set of group ids in ms, for timeline links)."""
    order = {c: i for i, c in enumerate(ordered_type_codes(m["type_code"] for m in markers))}
    items: list[tuple[float, int, int, str, str, str]] = []  # time, rank, id, chip code, chip color, html
    for m in markers:
        items.append((round(_marker_time(m), 3), order[m["type_code"]], m["event_id"], m["type_code"],
                      colors[m["type_code"]][1], _marker_card(m, frames, checks, base_dir, colors)))
    for tr in transitions:
        items.append((round(tr["time"], 3), 1000, tr["index"], "ASSET", "#6a6a8a",
                      _transition_card(tr, frames, checks, base_dir)))
    items.sort(key=lambda x: (x[0], x[1], x[2]))

    groups: dict[float, list[tuple]] = {}
    for it in items:
        groups.setdefault(it[0], []).append(it)

    out, keys = [], set()
    for t, group in groups.items():
        ms = round(t * 1000)
        keys.add(ms)
        chips = "".join(
            f'<span class="tp-chip" style="background:{it[4]}">{escape(it[3])}</span>' for it in group
        )
        cards = "".join(it[5] for it in group)
        out.append(f"""
    <section class="tp-group" id="tp-{ms}">
      <div class="tp-hdr">
        <span class="tp-time">{_fmt_time(t)}</span>
        <span class="tp-count">{len(group)} item{"s" if len(group) != 1 else ""}</span>
        {chips}
      </div>
      <div class="tp-cards">{cards}</div>
    </section>""")
    return "".join(out), keys


def _upid_panel(ev: dict) -> str:
    parts = []
    if ev.get("upid_hex"):
        parts.append(f'<div class="scte-field"><span class="k">upid</span><span class="v">type={ev.get("upid_type")} · {ev["upid_hex"]}</span></div>')
    if ev.get("segment_num") is not None:
        parts.append(f'<div class="scte-field"><span class="k">segment</span><span class="v">{ev["segment_num"]}/{ev.get("segments_expected") or 0}</span></div>')
    flags = [
        f"{k}={v}" if k == "device_restrictions" else k
        for k, v in (ev.get("flags") or {}).items() if v
    ]
    if flags:
        parts.append(f'<div class="scte-field"><span class="k">flags</span><span class="v">{" · ".join(flags)}</span></div>')
    if not parts:
        return ""
    return f'<div class="scte-panel">{"".join(parts)}</div>'


def _assets_row(assets: list[dict], total: float, linkable: set[int]) -> str:
    segs = []
    for i, a in enumerate(assets):
        pct_l = a["start"] / total * 100
        pct_w = max(0.05, (a["end"] - a["start"]) / total * 100)
        label = escape(str(a["asset_id"] or a["file"]))
        title = escape(f'{a["asset_id"]} ({a["file"]}) [{_fmt_time(a["start"])} → {_fmt_time(a["end"])}]')
        href = f' href="#tp-{round(a["start"] * 1000)}"' if round(a["start"] * 1000) in linkable else ""
        tag = "a" if href else "div"
        segs.append(f'<{tag} class="seg asset a{i % 2}"{href} style="left:{pct_l:.4f}%;width:{pct_w:.4f}%" title="{title}">{label}</{tag}>')
    return f'<div class="tl-row">{"".join(segs)}</div>'


def _timeline_bar(markers: list[dict], total: float, assets: Optional[list[dict]] = None,
                  linkable: Optional[set[int]] = None) -> str:
    if not total:
        return ""
    spans = [m for m in markers if m.get("start") is not None and m.get("stop") is not None]
    by_type: dict[str, list[dict]] = {}
    for m in spans:
        by_type.setdefault(m["type_code"], []).append(m)
    colors = type_colors(by_type)

    rows = [_assets_row(assets, total, linkable or set())] if assets else []
    for code in ordered_type_codes(by_type):
        bg, border, fg = colors[code]
        lane_ends: list[float] = []
        lanes: list[list[str]] = []
        for m in sorted(by_type[code], key=lambda x: x["start"]["pts_seconds"]):
            lo = m["start"]["pts_seconds"]
            hi = m["stop"]["pts_seconds"]
            for i, last in enumerate(lane_ends):
                if last <= lo:
                    lane = i
                    lane_ends[i] = hi
                    break
            else:
                lane = len(lane_ends)
                lane_ends.append(hi)
                lanes.append([])
            pct_l = lo / total * 100
            pct_w = max(0.05, (hi - lo) / total * 100)
            title = escape(f'#{m["event_id"]} {m["type_name"]} [{_fmt_time(lo)} → {_fmt_time(hi)}]')
            lanes[lane].append(
                f'<a class="seg" href="#marker-{m["event_id"]}" style="left:{pct_l:.4f}%;width:{pct_w:.4f}%;background:{bg};border-color:{border};color:{fg}" title="{title}">{escape(code)}</a>'
            )
        rows.extend(f'<div class="tl-row">{"".join(segs)}</div>' for segs in lanes)

    # Instant / point markers as ticks on their own row.
    ticks = []
    for m in markers:
        if m.get("start") is None or m.get("stop") is not None:
            continue
        pct_l = m["start"]["pts_seconds"] / total * 100
        title = escape(f'#{m["event_id"]} {m["type_name"]} @ {_fmt_time(m["start"]["pts_seconds"])}')
        ticks.append(f'<a class="tick" href="#marker-{m["event_id"]}" style="left:{pct_l:.4f}%" title="{title}"></a>')
    if ticks:
        rows.append(f'<div class="tl-row ticks">{"".join(ticks)}</div>')

    return f'<div class="tl-bar">{"".join(rows)}</div>'


def render_html(report: dict, base_dir: Optional[Path] = None) -> str:
    """Render a self-contained HTML report from a krogh JSON report.

    `base_dir` is the directory `report['frames'][...]['file']` paths are
    relative to (defaults to wherever the report's own frames/ dir would
    be, i.e. the directory the JSON was written into) -- pass it explicitly
    when rendering a JSON that's been moved or read from elsewhere.
    """
    src = report["source"]
    markers = report["markers"]
    frames = report.get("frames", {})
    checks = report.get("checks", [])
    summary = report.get("summary", {})
    total = src.get("duration") or 0.0

    colors = type_colors(m["type_code"] for m in markers)
    tl = report.get("timeline") or {}
    cards_html, linkable = _grouped_cards(markers, tl.get("transitions", []), frames, checks, base_dir, colors)
    timeline_html = _timeline_bar(markers, total, tl.get("assets"), linkable)

    checks_rows = "".join(
        f'<tr class="{"fail" if not c["pass"] else "pass"}">'
        f'<td>{_check_event_label(c)}</td><td>{c["boundary"]}</td><td>{c["check"]}</td>'
        f'<td class="result">{"✓ pass" if c["pass"] else "✗ fail"}</td><td>{c["detail"]}</td></tr>'
        for c in checks
    )

    tl_info = report.get("timeline")
    exp_info = report.get("expected")
    fps = src.get("fps")
    meta_html = meta_table_html([
        ("Video", f"{src.get('codec', '?')} {src.get('width')}×{src.get('height')}" + (f" @ {fps:g} fps" if fps else "")),
        ("Duration", _fmt_time(total)),
        ("SCTE-35 markers", str(summary.get("marker_count", 0))),
        ("Asset joins without a marker", str(len(tl_info["transitions"])) if tl_info else "unknown (no timeline.json)"),
        ("Expected markers", f"{exp_info['name']} ({exp_info['entries']} entries)" if exp_info else "none (independent scan only)"),
        ("Scan", "SCTE-35 table 0xFC, " + (f"PID {src['scte35_pid']}" if src.get("scte35_pid") else "all PIDs")),
    ])

    failed = summary.get("checks_failed", 0)
    overall_badge = (
        f'<span class="badge bad">{failed} check(s) failed</span>' if failed
        else '<span class="badge ok">all checks passed</span>'
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inspector Krogh — {src.get("name", "")}</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0c0c14;color:#c4c4d4;font-family:"JetBrains Mono","Fira Code","SF Mono",monospace;font-size:13px;line-height:1.5;padding:28px}}
header{{display:flex;flex-wrap:wrap;align-items:baseline;gap:16px;border-bottom:1px solid #252536;padding-bottom:16px;margin-bottom:24px}}
h1{{font-size:19px;color:#e8e8f4;letter-spacing:-.5px}}
.badge{{font-size:10px;font-weight:700;letter-spacing:.6px;padding:3px 9px;border-radius:10px;text-transform:uppercase}}
.badge.ok{{background:#0b3318;color:#66ffaa}}
.badge.bad{{background:#3a0d0d;color:#ff6666}}

.tl-wrap{{margin-bottom:32px}}
.tl-wrap h2, .checks-wrap h2, .markers-wrap h2{{font-size:14px;font-weight:700;text-transform:uppercase;letter-spacing:1.4px;color:#b4b4d0;margin-bottom:12px}}
.tl-bar{{display:flex;flex-direction:column;gap:2px;background:#0e0e1a;border:1px solid #232336;border-radius:4px;padding:4px}}
.tl-row{{position:relative;height:22px}}
.tl-row.ticks{{height:12px}}
.seg{{position:absolute;top:0;height:100%;text-decoration:none;cursor:pointer;border:1px solid;border-radius:2px;font-size:9px;display:flex;align-items:center;justify-content:center;overflow:hidden;white-space:nowrap}}
.seg:hover{{filter:brightness(1.35)}}
.seg.asset{{background:#22223a;border-color:#3a3a5a;color:#c4c4d4}}
.seg.asset.a1{{background:#2c2c48}}
div.seg.asset{{cursor:default}}
div.seg.asset:hover{{filter:none}}
.tick{{position:absolute;top:0;width:10px;margin-left:-5px;height:100%;cursor:pointer;background:linear-gradient(#ffaa44,#ffaa44) center/2px 100% no-repeat}}
.tick:hover{{background:linear-gradient(#ffd08a,#ffd08a) center/4px 100% no-repeat}}
html{{scroll-behavior:smooth}}
.marker-card{{scroll-margin-top:70px}}
.marker-card:target{{animation:flash 1.6s ease-out}}
@keyframes flash{{0%{{box-shadow:0 0 0 3px var(--t-fg),0 4px 18px rgba(0,0,0,.45)}}100%{{box-shadow:0 0 0 3px transparent,0 4px 18px rgba(0,0,0,.45)}}}}

.checks-wrap{{margin-bottom:32px}}
table.checks-table{{width:100%;border-collapse:collapse;font-size:12px}}
.checks-table th{{background:#161626;color:#b4b4e0;text-align:left;font-weight:600;padding:6px 10px;border-bottom:2px solid #2a2a3d}}
.checks-table td{{padding:5px 10px;border-bottom:1px solid #1a1a28;color:#b8b8d0;text-align:left}}
.checks-table td.result{{font-weight:700;white-space:nowrap}}
.checks-table tr.pass td.result{{color:#66ffaa}}
.checks-table tr.fail td.result{{color:#ff5555}}
.checks-table tr.fail td{{color:#ff8888}}

.tp-group{{position:relative;scroll-margin-top:16px}}
.tp-group + .tp-group{{margin-top:48px}}
.tp-hdr{{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:10px;padding:8px 0 10px;margin-bottom:8px;background:#0c0c14;border-bottom:2px solid #33334d}}
.tp-time{{font-size:16px;font-weight:700;color:#e8e8f4;letter-spacing:.5px}}
.tp-count{{font-size:11px;color:#b0b0cc;margin-right:6px}}
.tp-chip{{font-size:10px;font-weight:700;padding:1px 7px;border-radius:3px;color:#fff}}
.tp-cards{{display:flex;flex-direction:column;gap:18px}}
.marker-card{{border:1px solid #33334d;border-left:6px solid var(--t-border);border-radius:8px;overflow:hidden;background:#0f0f1a;box-shadow:0 4px 18px rgba(0,0,0,.45)}}
.marker-hdr{{display:flex;align-items:center;flex-wrap:wrap;gap:10px;padding:10px 16px;background:linear-gradient(90deg,var(--t-bg),#141424 70%);border-bottom:1px solid var(--t-border)}}
.mid{{font-size:11px;font-weight:700;letter-spacing:1px;color:var(--t-fg);text-transform:uppercase}}
.type-badge{{font-size:10px;font-weight:700;padding:2px 7px;border-radius:3px;background:var(--t-border);color:#fff}}
.splice-badge{{font-size:9px;padding:2px 7px;border-radius:3px;text-transform:uppercase;background:#162016;color:#66dd88}}
.splice-badge.splice_insert{{background:#162016;color:#66dd88}}
.splice-badge.time_signal{{background:#141428;color:#8fa8ee}}
.mname{{color:var(--t-fg);font-weight:600}}
.dur{{color:#b0b0cc;font-size:11px}}
.hdr-status{{margin-left:auto}}
.assets{{display:flex;gap:4px;flex-wrap:wrap}}
.asset-chip{{font-size:10px;padding:1px 7px;border-radius:3px;background:#1d1d33;border:1px solid #33334d;color:#c4c4d4}}

.bh{{display:flex;align-items:center;gap:14px;padding:8px 16px;color:#b0b0cc}}
.tag{{font-size:10px;font-weight:700;letter-spacing:.9px;padding:2px 7px;border-radius:3px}}
.tag.start{{background:#0b3318;color:#66ffaa}}
.tag.stop{{background:#3a1810;color:#ff9955}}
.tag.join{{background:#2a2a40;color:#d4d4e4}}
.pts{{color:#9a9ab8;font-size:11px}}

.frames{{display:flex;align-items:center;gap:4px;padding:10px 16px;overflow-x:auto}}
.span-row{{display:flex;align-items:flex-start;gap:8px;padding:0 0 4px;overflow-x:auto}}
.boundary{{flex-shrink:0}}
.span-gap{{flex-shrink:0;align-self:center;display:flex;flex-direction:column;align-items:center;justify-content:center;min-width:72px;color:#8f8fae}}
.span-gap .dots{{font-size:22px;line-height:1}}
.span-gap .gap-dur{{font-size:10px}}
.frame{{position:relative;display:flex;flex-direction:column;align-items:center;gap:4px;flex-shrink:0}}
.frame.mark-start::before,.frame.mark-stop::before,.frame.mark-join::before{{content:"";position:absolute;left:-3px;top:-6px;bottom:-4px;width:2px;border-radius:1px}}
.frame.mark-join::before{{background:#d4d4e4}}
.frame.mark-start::before{{background:#66ffaa}}
.frame.mark-stop::before{{background:#ff9955}}
.frame img{{display:block;border-radius:3px;border:2px solid transparent;opacity:.7;width:104px;height:auto}}
.frame.idr img{{opacity:1;border-color:#ff3b3b}}
.frame-label{{font-size:10px;color:#9a9ab8;display:flex;gap:4px;align-items:center}}
.idr-badge{{font-size:9px;background:#2e1010;color:#ff7777;border:1px solid #5c1a1a;padding:0 4px;border-radius:2px}}
.no-frame, .no-frames-note{{color:#9a9ab8;font-size:11px;padding:8px}}

.checks{{display:flex;flex-direction:column;gap:2px;padding:0 16px 10px}}
.check{{display:flex;gap:10px;font-size:11px}}
.check.pass .check-name{{color:#66dd88}}
.check.fail .check-name{{color:#ff6666}}
.check-detail{{color:#a0a0be}}

.scte-panel{{display:flex;flex-wrap:wrap;gap:4px 20px;padding:6px 16px 10px}}
.scte-field{{display:flex;gap:6px;font-size:11px}}
.scte-field .k{{color:#9a9ab8}}
.scte-field .v{{color:#b0b0f0}}
#zoom{{position:fixed;z-index:1000;pointer-events:none;display:none;border:2px solid #ff3b3b;border-radius:4px;background:#000;box-shadow:0 8px 32px rgba(0,0,0,.7)}}
#zoom img{{display:block;width:320px;height:auto}}
{HEADER_CSS}{META_CSS}
</style>
</head>
<body>
<header>
  {header_id_html("SCTE-35 marker verification — detailed report", src.get("name", ""))}
  {meta_html}
  {overall_badge}
</header>

<div class="tl-wrap">
  <h2>Timeline</h2>
  {timeline_html}
</div>

<div class="checks-wrap">
  <h2>Checks</h2>
  <table class="checks-table">
    <thead><tr><th>Event</th><th>Boundary</th><th>Check</th><th>Result</th><th>Detail</th></tr></thead>
    <tbody>{checks_rows or '<tr><td colspan="5" class="no-frame">no checks (no markers found)</td></tr>'}</tbody>
  </table>
</div>

<div class="markers-wrap">
  <h2>Markers</h2>
  {cards_html or '<div class="no-frame">No SCTE-35 markers found in this file.</div>'}
</div>
<div id="zoom"><img alt=""></div>
<script>
(function(){{
  var z=document.getElementById('zoom'), zi=z.querySelector('img');
  document.addEventListener('mouseover',function(e){{
    var img=e.target.closest&&e.target.closest('.frame img');
    if(!img){{z.style.display='none';return;}}
    zi.src=img.src; z.style.display='block';
    var r=img.getBoundingClientRect(), w=z.offsetWidth, h=z.offsetHeight;
    var x=Math.max(8,Math.min(innerWidth-w-8,r.left+r.width/2-w/2));
    var y=r.top-h-8; if(y<8) y=Math.min(innerHeight-h-8,r.bottom+8);
    z.style.left=x+'px'; z.style.top=y+'px';
  }});
  document.addEventListener('scroll',function(){{z.style.display='none';}},true);
}})();
</script>
</body>
</html>"""
