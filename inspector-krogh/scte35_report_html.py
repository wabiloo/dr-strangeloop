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

from krogh_common import HEADER_CSS, header_id_html, ordered_type_codes, type_colors


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
        <span class="hdr-status">{_check_badge(m_checks)}</span>
      </div>
      <div class="span-row">{body}</div>
    </div>"""


def _upid_panel(ev: dict) -> str:
    parts = []
    if ev.get("upid_hex"):
        parts.append(f'<div class="scte-field"><span class="k">upid</span><span class="v">type={ev.get("upid_type")} · {ev["upid_hex"]}</span></div>')
    if ev.get("segment_num") is not None:
        parts.append(f'<div class="scte-field"><span class="k">segment</span><span class="v">{ev["segment_num"]}/{ev.get("segments_expected") or 0}</span></div>')
    flags = [k for k, v in (ev.get("flags") or {}).items() if v]
    if flags:
        parts.append(f'<div class="scte-field"><span class="k">flags</span><span class="v">{" · ".join(flags)}</span></div>')
    if not parts:
        return ""
    return f'<div class="scte-panel">{"".join(parts)}</div>'


def _timeline_bar(markers: list[dict], total: float) -> str:
    if not total:
        return ""
    spans = [m for m in markers if m.get("start") is not None and m.get("stop") is not None]
    by_type: dict[str, list[dict]] = {}
    for m in spans:
        by_type.setdefault(m["type_code"], []).append(m)
    colors = type_colors(by_type)

    rows = []
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
    cards_html = "".join(_marker_card(m, frames, checks, base_dir, colors) for m in markers)
    timeline_html = _timeline_bar(markers, total)

    checks_rows = "".join(
        f'<tr class="{"fail" if not c["pass"] else "pass"}">'
        f'<td>#{c["event_id"]}</td><td>{c["boundary"]}</td><td>{c["check"]}</td>'
        f'<td class="result">{"✓ pass" if c["pass"] else "✗ fail"}</td><td>{c["detail"]}</td></tr>'
        for c in checks
    )

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
.meta{{color:#55556a;font-size:12px}}
.meta span{{margin-right:16px}}
.badge{{font-size:10px;font-weight:700;letter-spacing:.6px;padding:3px 9px;border-radius:10px;text-transform:uppercase}}
.badge.ok{{background:#0b3318;color:#66ffaa}}
.badge.bad{{background:#3a0d0d;color:#ff6666}}

.tl-wrap{{margin-bottom:32px}}
.tl-wrap h2, .checks-wrap h2, .markers-wrap h2{{font-size:10px;text-transform:uppercase;letter-spacing:1.2px;color:#44445a;margin-bottom:10px}}
.tl-bar{{display:flex;flex-direction:column;gap:2px;background:#0e0e1a;border:1px solid #232336;border-radius:4px;padding:4px}}
.tl-row{{position:relative;height:22px}}
.tl-row.ticks{{height:12px}}
.seg{{position:absolute;top:0;height:100%;text-decoration:none;cursor:pointer;border:1px solid;border-radius:2px;font-size:9px;display:flex;align-items:center;justify-content:center;overflow:hidden;white-space:nowrap}}
.seg:hover{{filter:brightness(1.35)}}
.tick{{position:absolute;top:0;width:10px;margin-left:-5px;height:100%;cursor:pointer;background:linear-gradient(#ffaa44,#ffaa44) center/2px 100% no-repeat}}
.tick:hover{{background:linear-gradient(#ffd08a,#ffd08a) center/4px 100% no-repeat}}
html{{scroll-behavior:smooth}}
.marker-card{{scroll-margin-top:16px}}
.marker-card:target{{animation:flash 1.6s ease-out}}
@keyframes flash{{0%{{box-shadow:0 0 0 3px var(--t-fg),0 4px 18px rgba(0,0,0,.45)}}100%{{box-shadow:0 0 0 3px transparent,0 4px 18px rgba(0,0,0,.45)}}}}

.checks-wrap{{margin-bottom:32px}}
table.checks-table{{width:100%;border-collapse:collapse;font-size:12px}}
.checks-table th{{background:#161626;color:#7777aa;text-align:left;font-weight:600;padding:6px 10px;border-bottom:2px solid #2a2a3d}}
.checks-table td{{padding:5px 10px;border-bottom:1px solid #1a1a28;color:#8888aa;text-align:left}}
.checks-table td.result{{font-weight:700;white-space:nowrap}}
.checks-table tr.pass td.result{{color:#66ffaa}}
.checks-table tr.fail td.result{{color:#ff5555}}
.checks-table tr.fail td{{color:#ff8888}}

.markers-wrap{{display:flex;flex-direction:column;gap:40px}}
.marker-card{{border:1px solid #33334d;border-left:6px solid var(--t-border);border-radius:8px;overflow:hidden;background:#0f0f1a;box-shadow:0 4px 18px rgba(0,0,0,.45)}}
.marker-hdr{{display:flex;align-items:center;flex-wrap:wrap;gap:10px;padding:10px 16px;background:linear-gradient(90deg,var(--t-bg),#141424 70%);border-bottom:1px solid var(--t-border)}}
.mid{{font-size:11px;font-weight:700;letter-spacing:1px;color:var(--t-fg);text-transform:uppercase}}
.type-badge{{font-size:10px;font-weight:700;padding:2px 7px;border-radius:3px;background:var(--t-border);color:#fff}}
.splice-badge{{font-size:9px;padding:2px 7px;border-radius:3px;text-transform:uppercase;background:#162016;color:#55bb55}}
.splice-badge.splice_insert{{background:#162016;color:#55bb55}}
.splice-badge.time_signal{{background:#141428;color:#5577cc}}
.mname{{color:var(--t-fg);font-weight:600}}
.dur{{color:#7a7a98;font-size:11px}}
.hdr-status{{margin-left:auto}}

.bh{{display:flex;align-items:center;gap:14px;padding:8px 16px;color:#7a7a9a}}
.tag{{font-size:10px;font-weight:700;letter-spacing:.9px;padding:2px 7px;border-radius:3px}}
.tag.start{{background:#0b3318;color:#66ffaa}}
.tag.stop{{background:#3a1810;color:#ff9955}}
.pts{{color:#44445a;font-size:11px}}

.frames{{display:flex;align-items:center;gap:4px;padding:10px 16px;overflow-x:auto}}
.span-row{{display:flex;align-items:flex-start;gap:8px;padding:0 0 4px;overflow-x:auto}}
.boundary{{flex-shrink:0}}
.span-gap{{flex-shrink:0;align-self:center;display:flex;flex-direction:column;align-items:center;justify-content:center;min-width:72px;color:#3a3a52}}
.span-gap .dots{{font-size:22px;line-height:1}}
.span-gap .gap-dur{{font-size:10px}}
.frame{{position:relative;display:flex;flex-direction:column;align-items:center;gap:4px;flex-shrink:0}}
.frame.mark-start::before,.frame.mark-stop::before{{content:"";position:absolute;left:-3px;top:-6px;bottom:-4px;width:2px;border-radius:1px}}
.frame.mark-start::before{{background:#66ffaa}}
.frame.mark-stop::before{{background:#ff9955}}
.frame img{{display:block;border-radius:3px;border:2px solid transparent;opacity:.7;width:104px;height:auto}}
.frame.idr img{{opacity:1;border-color:#ff3b3b}}
.frame-label{{font-size:10px;color:#44445a;display:flex;gap:4px;align-items:center}}
.idr-badge{{font-size:8px;background:#2e1010;color:#e05555;border:1px solid #5c1a1a;padding:0 4px;border-radius:2px}}
.no-frame, .no-frames-note{{color:#333;font-size:11px;padding:8px}}

.checks{{display:flex;flex-direction:column;gap:2px;padding:0 16px 10px}}
.check{{display:flex;gap:10px;font-size:11px}}
.check.pass .check-name{{color:#55bb55}}
.check.fail .check-name{{color:#ff6666}}
.check-detail{{color:#555}}

.scte-panel{{display:flex;flex-wrap:wrap;gap:4px 20px;padding:6px 16px 10px}}
.scte-field{{display:flex;gap:6px;font-size:11px}}
.scte-field .k{{color:#2e2e4a}}
.scte-field .v{{color:#6666aa}}
#zoom{{position:fixed;z-index:1000;pointer-events:none;display:none;border:2px solid #ff3b3b;border-radius:4px;background:#000;box-shadow:0 8px 32px rgba(0,0,0,.7)}}
#zoom img{{display:block;width:320px;height:auto}}
{HEADER_CSS}
</style>
</head>
<body>
<header>
  {header_id_html("SCTE-35 marker verification — detailed report", src.get("name", ""))}
  <div class="meta">
    <span>{src.get("codec", "?")} {src.get("width")}×{src.get("height")}</span>
    <span>{_fmt_time(total)}</span>
    <span>{summary.get("marker_count", 0)} marker(s)</span>
    <span>table 0xFC{f" · pid {src.get('scte35_pid')}" if src.get("scte35_pid") else " · all PIDs"}</span>
  </div>
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
