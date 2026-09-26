#!/usr/bin/env python3
"""
frame_extractor.py

Extracts every frame from a video, overlays frame number / type (I/P/B) /
timestamp, then generates a self-contained HTML timeline viewer.

Requirements
------------
  ffmpeg + ffprobe  (must be on PATH)

Usage
-----
  uv run frame-extractor video.mp4
  uv run frame-extractor video.mp4 --width 480 --workers 8
  uv run frame-extractor video.mp4 --no-overlay --output ./out/
"""

import sys

import argparse
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.text import Text

from krogh_common import HEADER_CSS, header_id_html

console = Console()


# ── tiny helpers ──────────────────────────────────────────────────────────────

def _run(cmd: list[str], label: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        console.print(f"[bold red][{label}] failed:[/]\n{proc.stderr.strip()}")
        sys.exit(1)
    return proc


def _fps(rate: str) -> float:
    try:
        n, d = rate.split("/")
        return round(int(n) / int(d), 4) if int(d) else 0.0
    except Exception:
        return 0.0


def _hms(secs: float) -> str:
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = secs % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


# ── ffprobe ───────────────────────────────────────────────────────────────────

def probe_video(path: str) -> dict:
    proc = _run(
        ["ffprobe", "-v", "error",
         "-select_streams", "v:0",
         "-show_streams", "-show_format",
         "-of", "json", path],
        "ffprobe",
    )
    data = json.loads(proc.stdout)
    s = data.get("streams", [{}])[0]
    f = data.get("format", {})
    dur = float(f.get("duration") or s.get("duration") or 0)
    return {
        "codec":    s.get("codec_name", "?"),
        "profile":  s.get("profile", ""),
        "width":    s.get("width", 0),
        "height":   s.get("height", 0),
        "duration": dur,
        "fps":      _fps(s.get("r_frame_rate", "0/1")),
        "br":       int(f.get("bit_rate") or 0),
    }


def probe_frames(path: str) -> list[dict]:
    """Return [{type, pts}] for every video frame in presentation order."""
    proc = _run(
        ["ffprobe", "-v", "error",
         "-select_streams", "v:0",
         "-show_frames",
         "-show_entries", "frame=pict_type,best_effort_timestamp_time,pts_time",
         "-of", "json", path],
        "ffprobe",
    )
    raw = json.loads(proc.stdout).get("frames", [])
    out = []
    for r in raw:
        ts_str = r.get("best_effort_timestamp_time") or r.get("pts_time") or "0"
        try:
            ts = float(ts_str)
        except ValueError:
            ts = 0.0
        out.append({"type": r.get("pict_type", "?"), "pts": ts})
    return out


# ── ffmpeg extraction ─────────────────────────────────────────────────────────

def extract_frames(
    video: str,
    out_dir: Path,
    width: int,
    total: int,
    progress: Progress,
    task_id,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-i", video,
        "-vf", f"scale={width}:-2",
        "-q:v", "3", "-y",
        "-progress", "pipe:1",
        "-nostats",
        str(out_dir / "frame_%06d.jpg"),
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    for line in proc.stdout:
        if line.startswith("frame="):
            try:
                n = int(line.split("=", 1)[1])
                progress.update(task_id, completed=min(n, total))
            except ValueError:
                pass
    proc.wait()
    if proc.returncode != 0:
        console.print("[bold red][ffmpeg] extraction failed[/]")
        sys.exit(1)
    progress.update(task_id, completed=total)


# ── PIL overlay ───────────────────────────────────────────────────────────────

_TYPE_BG = {"I": (200, 45, 45), "P": (40, 175, 75), "B": (55, 120, 230)}

_FONT_SEARCH = [
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/Library/Fonts/Courier New.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeMono.ttf",
]


def _get_font(size: int):
    from PIL import ImageFont
    for p in _FONT_SEARCH:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _overlay_one(task: tuple) -> None:
    path, idx, ftype, ts, gop = task
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return

    img = Image.open(path).convert("RGB")
    w, h = img.size
    bar_h = max(18, h // 16)

    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, w, bar_h], fill=(12, 12, 12))

    bg = _TYPE_BG.get(ftype, (150, 150, 150))
    badge_w = bar_h + 8
    draw.rectangle([0, 0, badge_w, bar_h], fill=bg)

    font_size = max(10, bar_h - 5)
    font = _get_font(font_size)

    try:
        bb = font.getbbox(ftype)
        tx = (badge_w - (bb[2] - bb[0])) // 2
        ty = (bar_h  - (bb[3] - bb[1])) // 2 - 1
    except AttributeError:
        tx, ty = 4, 2
    draw.text((tx, ty), ftype, fill=(255, 255, 255), font=font)

    info = f"  #{idx:>6}    {_hms(ts)}    GOP {gop}"
    draw.text((badge_w + 6, (bar_h - font_size) // 2), info,
              fill=(210, 210, 210), font=font)

    img.save(path, "JPEG", quality=80, optimize=True)


def apply_overlays(
    frames: list[dict],
    frames_dir: Path,
    workers: int,
    progress: Progress,
    task_id,
) -> None:
    tasks = [
        (frames_dir / f"frame_{f['idx']:06d}.jpg",
         f["idx"], f["type"], f["pts"], f["gop"])
        for f in frames
        if (frames_dir / f"frame_{f['idx']:06d}.jpg").exists()
    ]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_overlay_one, t) for t in tasks]
        for fut in as_completed(futs):
            fut.result()
            progress.advance(task_id)


# ── HTML generation ───────────────────────────────────────────────────────────

def generate_html(
    frames: list[dict],
    info: dict,
    out_html: Path,
    frames_subdir: str,
    video_name: str,
) -> None:
    i_cnt = sum(1 for f in frames if f["type"] == "I")
    p_cnt = sum(1 for f in frames if f["type"] == "P")
    b_cnt = sum(1 for f in frames if f["type"] == "B")
    total = len(frames)
    avg_gop = round(total / i_cnt, 1) if i_cnt else "—"

    js_frames = json.dumps(
        [{"i": f["idx"], "t": f["type"], "ts": round(f["pts"], 4), "g": f["gop"]}
         for f in frames],
        separators=(",", ":"),
    )

    # Use repr() so Python escapes backslashes → JS sees \\d which is valid regex \d
    js_frames_dir = json.dumps(frames_subdir)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inspector Krogh — {video_name}</title>
<style>
*,::before,::after{{box-sizing:border-box;margin:0;padding:0}}
html{{font-family:'SF Mono','Menlo','Consolas','Liberation Mono',monospace;font-size:13px}}
body{{background:#0e0e12;color:#ccc;height:100vh;display:flex;flex-direction:column;overflow:hidden}}

/* ── header ── */
#hdr{{background:#16161e;border-bottom:1px solid #2a2a3a;padding:10px 16px;flex-shrink:0}}
#hdr h1{{font-size:14px;font-weight:600;color:#e0e0f0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
#meta{{display:flex;flex-wrap:wrap;gap:8px 20px;margin-top:6px;font-size:11px;color:#666}}
#meta span b{{color:#999}}
#controls{{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-top:10px}}
.sep{{width:1px;height:18px;background:#2a2a3a;margin:0 4px;flex-shrink:0}}

.btn{{padding:3px 10px;border-radius:4px;border:1px solid #2e2e3e;background:#1a1a28;color:#888;
      cursor:pointer;font-size:11px;font-family:inherit;transition:all .15s;white-space:nowrap}}
.btn:hover{{background:#252538;color:#ccc}}
.btn.active{{border-color:currentColor;font-weight:600}}
.btn.I{{color:#e05555;border-color:#5c1a1a}} .btn.I.active{{background:#2e1010}}
.btn.P{{color:#4dc878;border-color:#1a5c2e}} .btn.P.active{{background:#102410}}
.btn.B{{color:#5b9bef;border-color:#1a3d6e}} .btn.B.active{{background:#0e1e38}}

.inp{{padding:3px 8px;border-radius:4px;border:1px solid #2e2e3e;background:#14141e;
      color:#ccc;font-size:11px;font-family:inherit}}
.inp::placeholder{{color:#3a3a4e}}
.inp:focus{{outline:none;border-color:#444}}
#search{{width:100px}}
#timejump{{width:148px}}
#jump-result{{font-size:10px;color:#5577bb;white-space:nowrap;transition:opacity .3s}}
#info-bar{{margin-left:auto;font-size:10px;color:#3a3a5a;white-space:nowrap}}

/* ── list ── */
#list{{flex:1;overflow-y:auto;padding:4px 0;scroll-behavior:smooth}}
#list::-webkit-scrollbar{{width:5px}}
#list::-webkit-scrollbar-track{{background:#0a0a0e}}
#list::-webkit-scrollbar-thumb{{background:#252535;border-radius:3px}}

/* ── GOP group ── */
.gop-group{{display:flex;border-bottom:2px solid #0e080a;margin-bottom:1px}}
.gop-group.hidden{{display:none}}

.gop-sidebar{{
  width:50px;flex-shrink:0;
  background:#0a0608;
  border-left:3px solid #8b2020;
  display:flex;flex-direction:column;
  align-items:center;justify-content:center;
  padding:6px 2px;gap:4px;
}}
.gop-num{{
  font-size:9px;font-weight:700;color:#c03030;letter-spacing:.5px;
  writing-mode:vertical-rl;text-orientation:mixed;
  transform:rotate(180deg);
  white-space:nowrap;
}}
.gop-cnt{{font-size:9px;color:#4a2020}}
.gop-ts{{font-size:8px;color:#3a1515;writing-mode:vertical-rl;transform:rotate(180deg)}}
.gop-frames{{flex:1;min-width:0}}

/* ── frame rows ── */
.row{{display:flex;align-items:stretch;border-bottom:1px solid #141418;
      cursor:pointer;transition:background .08s;position:relative}}
.row:hover{{background:#14141c}}
.row.selected{{background:#141e32;outline:2px solid #2d4a99;outline-offset:-2px}}
.row.hidden{{display:none}}

.stripe{{width:3px;flex-shrink:0}}
.stripe.I{{background:#e05555}} .stripe.P{{background:#4dc878}} .stripe.B{{background:#5b9bef}}

.thumb-wrap{{flex-shrink:0;background:#08080c;display:flex;align-items:center;overflow:hidden}}
.thumb-wrap img{{width:160px;height:auto;display:block}}

.frame-info{{flex:1;padding:5px 12px;display:flex;flex-direction:column;justify-content:center;min-width:0}}
.fi-row{{display:flex;align-items:center;gap:8px}}
.badge{{display:inline-block;padding:1px 6px;border-radius:3px;font-size:11px;font-weight:700}}
.badge.I{{background:#2e1010;color:#e05555;border:1px solid #5c1a1a}}
.badge.P{{background:#102410;color:#4dc878;border:1px solid #1a5c2e}}
.badge.B{{background:#0e1e38;color:#5b9bef;border:1px solid #1a3d6e}}
.fn{{font-size:12px;color:#777}}
.fn b{{color:#bbb}}
.ts{{font-size:11px;color:#5a70aa;margin-top:3px;font-variant-numeric:tabular-nums}}
.gop-tag{{font-size:10px;color:#333;margin-top:2px}}

/* ── lightbox ── */
#lb{{display:none;position:fixed;inset:0;background:#000000aa;backdrop-filter:blur(4px);
     z-index:100;align-items:center;justify-content:center;flex-direction:column;gap:14px}}
#lb.open{{display:flex}}

#lb-frame{{position:relative;display:inline-block}}
#lb-img{{
  max-width:90vw;max-height:76vh;
  border-radius:6px;box-shadow:0 16px 64px #000e;
  border:3px solid #555;  /* overridden per frame type */
  display:block;
}}
#lb-badge{{
  position:absolute;top:-1px;left:-1px;
  padding:5px 14px;border-radius:5px 0 8px 0;
  font-size:16px;font-weight:700;letter-spacing:1px;
  border-right:2px solid transparent;
  border-bottom:2px solid transparent;
}}
#lb-badge.I{{background:#2e1010;color:#e05555;border-color:#5c1a1a}}
#lb-badge.P{{background:#102410;color:#4dc878;border-color:#1a5c2e}}
#lb-badge.B{{background:#0e1e38;color:#5b9bef;border-color:#1a3d6e}}

#lb-info{{color:#bbb;font-size:12px;text-align:center;line-height:1.8}}
#lb-nav{{display:flex;gap:8px;align-items:center}}
#lb-nav button{{
  padding:5px 14px;border:1px solid #333;background:#1a1a28;color:#bbb;
  border-radius:4px;cursor:pointer;font-size:11px;font-family:inherit;white-space:nowrap}}
#lb-nav button:hover{{background:#252538;color:#fff}}
#lb-nav .iframe-btn{{border-color:#5c1a1a;color:#c03030}}
#lb-nav .iframe-btn:hover{{background:#2e1010;color:#e05555}}
#lb-nav .sep{{width:1px;height:20px;background:#2a2a3a}}
#lb-close{{position:absolute;top:14px;right:18px;font-size:20px;color:#555;cursor:pointer;line-height:1}}
#lb-close:hover{{color:#fff}}

#empty{{display:none;padding:60px;text-align:center;color:#333;font-size:13px}}
{HEADER_CSS}
</style>
</head>
<body>

<div id="hdr">
  {header_id_html("Frame timeline — GOP structure and every frame (I/P/B)", video_name)}
  <div id="meta">
    <span><b>Codec</b> {info['codec']} {info['profile']}</span>
    <span><b>Resolution</b> {info['width']}×{info['height']}</span>
    <span><b>FPS</b> {info['fps']}</span>
    <span><b>Duration</b> {_hms(info['duration'])}</span>
    <span><b>Bitrate</b> {info['br']//1000 if info['br'] else '?'} kbps</span>
    <span><b>Frames</b> {total}&nbsp;
      (<b style="color:#e05555">I</b>&nbsp;{i_cnt}&nbsp;
       <b style="color:#4dc878">P</b>&nbsp;{p_cnt}&nbsp;
       <b style="color:#5b9bef">B</b>&nbsp;{b_cnt})</span>
    <span><b>Avg GOP</b> {avg_gop} frames</span>
  </div>
  <div id="controls">
    <button class="btn active" id="btn-ALL" onclick="setFilter('ALL')">All</button>
    <button class="btn I"      id="btn-I"   onclick="setFilter('I')">I-frames</button>
    <button class="btn P"      id="btn-P"   onclick="setFilter('P')">P-frames</button>
    <button class="btn B"      id="btn-B"   onclick="setFilter('B')">B-frames</button>
    <button class="btn"        id="btn-gop" onclick="toggleGOP()">GOP only</button>
    <div class="sep"></div>
    <input class="inp" id="search" type="text" placeholder="frame #…"
           oninput="onFrameSearch(this.value)">
    <div class="sep"></div>
    <input class="inp" id="timejump" type="text"
           placeholder="00:01:30 or 90.5s"
           onkeydown="if(event.key==='Enter')goToTime(this.value)">
    <button class="btn" onclick="goToTime(document.getElementById('timejump').value)">Go&nbsp;&#8594;</button>
    <span id="jump-result"></span>
    <span id="info-bar"></span>
  </div>
</div>

<div id="list"></div>
<div id="empty">No frames match the current filter.</div>

<div id="lb">
  <div id="lb-close" onclick="closeLB()">&#10005;</div>
  <div id="lb-frame">
    <img id="lb-img" src="" alt="">
    <div id="lb-badge" class="I">I</div>
  </div>
  <div id="lb-info"></div>
  <div id="lb-nav">
    <button class="iframe-btn" onclick="lbStepIFrame(-1)" title="Previous I-frame (keyboard: [)">&#9198; Prev I</button>
    <button onclick="lbStep(-1)">&#8592; Prev</button>
    <button onclick="lbStep(+1)">Next &#8594;</button>
    <button class="iframe-btn" onclick="lbStepIFrame(+1)" title="Next I-frame (keyboard: ])">Next I &#9197;</button>
  </div>
</div>

<script>
const FRAMES_DIR = {js_frames_dir};
const DATA = {js_frames};
const PTS  = DATA.map(f => f.ts);  // sorted pts for binary search

const TYPE_COLOR = {{I:'#e05555', P:'#4dc878', B:'#5b9bef'}};

let filter   = 'ALL';
let gopOnly  = false;
let searchFn = null;
let selected = 0;
let lbOpen   = false;

// ── DOM helpers ────────────────────────────────────────────────────────────

function rowId(frameIdx) {{ return 'r' + frameIdx; }}
function grpId(gopNum)   {{ return 'gopg-' + gopNum; }}
function imgSrc(frameIdx) {{
  return FRAMES_DIR + '/frame_' + String(frameIdx).padStart(6,'0') + '.jpg';
}}

// ── build list grouped by GOP ─────────────────────────────────────────────

(function buildList() {{
  // Pre-compute per-GOP stats
  const gopStats = {{}};
  DATA.forEach(f => {{
    if (!gopStats[f.g]) gopStats[f.g] = {{ count:0, startTs: f.ts }};
    gopStats[f.g].count++;
  }});

  const list = document.getElementById('list');
  const frag = document.createDocumentFragment();

  let curGOP = -1, group = null, framesDiv = null;

  DATA.forEach(f => {{
    if (f.g !== curGOP) {{
      if (group) frag.appendChild(group);
      curGOP = f.g;
      const st = gopStats[f.g];

      group = document.createElement('div');
      group.className = 'gop-group';
      group.id = grpId(f.g);

      const sidebar = document.createElement('div');
      sidebar.className = 'gop-sidebar';
      sidebar.innerHTML =
        '<div class="gop-num">GOP ' + f.g + '</div>' +
        '<div class="gop-cnt">' + st.count + ' fr</div>' +
        '<div class="gop-ts">' + fmtTimeShort(st.startTs) + '</div>';

      framesDiv = document.createElement('div');
      framesDiv.className = 'gop-frames';

      group.appendChild(sidebar);
      group.appendChild(framesDiv);
    }}

    framesDiv.appendChild(makeRow(f));
  }});

  if (group) frag.appendChild(group);
  list.appendChild(frag);
  updateInfoBar();
}})();

function makeRow(f) {{
  const div = document.createElement('div');
  div.className = 'row';
  div.id = rowId(f.i);
  div.dataset.type = f.t;
  div.innerHTML =
    '<div class="stripe ' + f.t + '"></div>' +
    '<div class="thumb-wrap"><img loading="lazy" src="' + imgSrc(f.i) + '" alt="frame ' + f.i + '"></div>' +
    '<div class="frame-info">' +
      '<div class="fi-row">' +
        '<span class="badge ' + f.t + '">' + f.t + '</span>' +
        '<span class="fn">Frame <b>#' + f.i + '</b></span>' +
      '</div>' +
      '<div class="ts">' + fmtTime(f.ts) + '</div>' +
    '</div>';
  div.addEventListener('click', () => selectAndOpen(f.i - 1));
  return div;
}}

// ── selection ──────────────────────────────────────────────────────────────

function selectRow(dataIdx, scrollMode) {{
  scrollMode = scrollMode || 'center';
  document.querySelector('.row.selected')?.classList.remove('selected');
  const f = DATA[dataIdx];
  if (!f) return;
  selected = dataIdx;
  const el = document.getElementById(rowId(f.i));
  if (el) {{
    el.classList.add('selected');
    el.scrollIntoView({{block: scrollMode, behavior: 'smooth'}});
  }}
}}

function visibleIndices() {{
  const result = [];
  DATA.forEach((f, i) => {{
    const el = document.getElementById(rowId(f.i));
    if (el && !el.classList.contains('hidden')) result.push(i);
  }});
  return result;
}}

function stepSelected(delta) {{
  const vis = visibleIndices();
  if (!vis.length) return;
  const pos = vis.indexOf(selected);
  const next = pos === -1
    ? vis[0]
    : vis[Math.max(0, Math.min(vis.length - 1, pos + delta))];
  selectRow(next, 'nearest');
  if (lbOpen) openLB(next);
}}

function selectAndOpen(dataIdx) {{
  selectRow(dataIdx, 'center');
  openLB(dataIdx);
}}

// ── time navigation ────────────────────────────────────────────────────────

function parseTimeStr(s) {{
  s = s.trim().replace(/s$/i, '');
  if (!s) return null;
  // Plain number
  if (/^[0-9]+([.][0-9]+)?$/.test(s)) return parseFloat(s);
  // HH:MM:SS or MM:SS (with optional fractional seconds)
  const parts = s.split(':').map(parseFloat);
  if (parts.some(isNaN)) return null;
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  return null;
}}

function goToTime(str) {{
  const t = parseTimeStr(str);
  const resultEl = document.getElementById('jump-result');
  if (t === null || isNaN(t) || t < 0) {{
    resultEl.textContent = str.trim() ? '✗ invalid time' : '';
    resultEl.style.color = '#884444';
    return;
  }}

  // Binary search for nearest frame globally
  let lo = 0, hi = DATA.length - 1;
  while (lo < hi) {{
    const mid = (lo + hi) >> 1;
    if (PTS[mid] < t) lo = mid + 1; else hi = mid;
  }}
  if (lo > 0 && Math.abs(PTS[lo-1] - t) <= Math.abs(PTS[lo] - t)) lo--;

  // Respect active filter: scan outward for nearest visible frame
  function isVis(i) {{
    if (i < 0 || i >= DATA.length) return false;
    const el = document.getElementById(rowId(DATA[i].i));
    return el && !el.classList.contains('hidden');
  }}

  let best = -1;
  if (isVis(lo)) {{
    best = lo;
  }} else {{
    for (let d = 1; d < DATA.length; d++) {{
      if (isVis(lo - d)) {{ best = lo - d; break; }}
      if (isVis(lo + d)) {{ best = lo + d; break; }}
    }}
  }}

  if (best === -1) {{
    resultEl.textContent = '✗ no visible frame near that time';
    resultEl.style.color = '#884444';
    return;
  }}

  const f = DATA[best];
  const diff = Math.abs(f.ts - t);
  resultEl.textContent =
    '→ #' + f.i + ' (' + f.t + ') at ' + fmtTime(f.ts) + (diff > 0.1 ? ' [nearest]' : '');
  resultEl.style.color = '#5577bb';

  selectRow(best, 'center');
  if (lbOpen) openLB(best);
}}

// ── filtering ──────────────────────────────────────────────────────────────

function applyFilter() {{
  let visible = 0;
  DATA.forEach(f => {{
    const el = document.getElementById(rowId(f.i));
    if (!el) return;
    const show = (filter === 'ALL' || f.t === filter)
              && (!gopOnly || f.t === 'I')
              && (!searchFn || searchFn(f));
    el.classList.toggle('hidden', !show);
    if (show) visible++;
  }});

  // Hide GOP group containers that have no visible rows
  document.querySelectorAll('.gop-group').forEach(g => {{
    g.classList.toggle('hidden', !g.querySelector('.row:not(.hidden)'));
  }});

  document.getElementById('empty').style.display = visible ? 'none' : 'block';
  updateInfoBar(visible);
}}

function updateInfoBar(visible) {{
  if (visible === undefined) visible = DATA.length;
  document.getElementById('info-bar').textContent =
    visible.toLocaleString() + ' / ' + DATA.length.toLocaleString() +
    ' frames · ↑↓ jk · Enter zoom · g goto · Esc close';
}}

function setFilter(f) {{
  filter = f;
  ['ALL','I','P','B'].forEach(t => {{
    document.getElementById('btn-' + t)?.classList.toggle('active', t === f);
  }});
  applyFilter();
}}

function toggleGOP() {{
  gopOnly = !gopOnly;
  document.getElementById('btn-gop').classList.toggle('active', gopOnly);
  applyFilter();
}}

function onFrameSearch(val) {{
  const v = val.trim();
  searchFn = v ? (f => f.i === parseInt(v, 10)) : null;
  applyFilter();
}}

// ── lightbox ───────────────────────────────────────────────────────────────

const lb      = document.getElementById('lb');
const lbImg   = document.getElementById('lb-img');
const lbInf   = document.getElementById('lb-info');
const lbBadge = document.getElementById('lb-badge');

function openLB(dataIdx) {{
  const f = DATA[dataIdx];
  if (!f) return;
  lbOpen   = true;
  selected = dataIdx;

  lbImg.src = imgSrc(f.i);

  const col = TYPE_COLOR[f.t] || '#888';
  lbImg.style.borderColor   = col;
  lbImg.style.boxShadow     = '0 16px 64px #000e, 0 0 0 1px ' + col + '44';
  lbBadge.textContent       = f.t;
  lbBadge.className         = 'lb-badge I';   // reset then set
  lbBadge.className         = 'lb-badge ' + f.t;

  lbInf.innerHTML =
    'Frame <b>#' + f.i + '</b>' +
    ' &nbsp;·&nbsp; <span style="color:' + col + ';font-weight:700">' + f.t + '-frame</span>' +
    ' &nbsp;·&nbsp; ' + fmtTime(f.ts) +
    ' &nbsp;·&nbsp; GOP ' + f.g;

  lb.classList.add('open');
}}

function closeLB() {{
  lbOpen = false;
  lb.classList.remove('open');
}}

function lbStep(delta) {{
  const vis = visibleIndices();
  if (!vis.length) return;
  const pos  = vis.indexOf(selected);
  const next = pos === -1
    ? vis[0]
    : vis[Math.max(0, Math.min(vis.length - 1, pos + delta))];
  selectRow(next, 'nearest');
  openLB(next);
}}

function lbStepIFrame(dir) {{
  const vis   = visibleIndices();
  const iIdxs = vis.filter(i => DATA[i].t === 'I');
  if (!iIdxs.length) return;
  let next;
  if (dir > 0) {{
    next = iIdxs.find(i => i > selected);
    if (next === undefined) next = iIdxs[iIdxs.length - 1];
  }} else {{
    next = [...iIdxs].reverse().find(i => i < selected);
    if (next === undefined) next = iIdxs[0];
  }}
  selectRow(next, 'nearest');
  openLB(next);
}}

lb.addEventListener('click', e => {{ if (e.target === lb) closeLB(); }});

// ── keyboard ───────────────────────────────────────────────────────────────

document.addEventListener('keydown', e => {{
  const tag = e.target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA') {{
    if (e.key === 'Escape') closeLB();
    return;
  }}
  switch (e.key) {{
    case 'ArrowDown': case 'j': e.preventDefault(); stepSelected(+1); break;
    case 'ArrowUp':   case 'k': e.preventDefault(); stepSelected(-1); break;
    case 'Enter':   if (!lbOpen) openLB(selected); break;
    case 'Escape':  closeLB(); break;
    case 'ArrowRight': if (lbOpen) {{ e.preventDefault(); lbStep(+1); }}     break;
    case 'ArrowLeft':  if (lbOpen) {{ e.preventDefault(); lbStep(-1); }}     break;
    case ']':          if (lbOpen) lbStepIFrame(+1); break;
    case '[':          if (lbOpen) lbStepIFrame(-1); break;
    case 'i': setFilter(filter === 'I' ? 'ALL' : 'I'); break;
    case 'p': setFilter(filter === 'P' ? 'ALL' : 'P'); break;
    case 'b': setFilter(filter === 'B' ? 'ALL' : 'B'); break;
    case 'g': e.preventDefault(); document.getElementById('timejump').focus(); break;
  }}
}});

// ── utils ──────────────────────────────────────────────────────────────────

function fmtTime(s) {{
  const h   = Math.floor(s / 3600);
  const m   = Math.floor((s % 3600) / 60);
  const sec = (s % 60).toFixed(3).padStart(6, '0');
  return String(h).padStart(2,'0') + ':' + String(m).padStart(2,'0') + ':' + sec;
}}

function fmtTimeShort(s) {{
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  return h > 0
    ? h + ':' + String(m).padStart(2,'0') + ':' + String(sec).padStart(2,'0')
    : m + ':' + String(sec).padStart(2,'0');
}}

selectRow(0, 'start');
</script>
</body>
</html>"""

    out_html.write_text(html, encoding="utf-8")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Extract video frames with I/P/B overlays and build an HTML timeline."
    )
    ap.add_argument("video", help="Input video file")
    ap.add_argument("--width",      type=int, default=320,
                    help="Thumbnail width in pixels (default: 320)")
    ap.add_argument("--output",     default=None,
                    help="Output directory (default: <video_stem>_timeline/)")
    ap.add_argument("--workers",    type=int, default=4,
                    help="Parallel workers for overlay step (default: 4)")
    ap.add_argument("--no-overlay", action="store_true",
                    help="Skip PIL overlay (faster, no text burned into frames)")
    args = ap.parse_args()

    video_path = Path(args.video)
    if not video_path.exists():
        console.print(f"[red]File not found:[/] {video_path}")
        sys.exit(1)

    out_root   = Path(args.output) if args.output else video_path.parent / f"{video_path.stem}_timeline"
    frames_dir = out_root / "frames"
    html_path  = out_root / "index.html"

    console.print()
    console.print(Panel(
        f"[bold]{video_path.name}[/]\n"
        f"[dim]→ {out_root.resolve()}[/]",
        title="[bold blue]🕵️ Frame Extractor[/]",
        border_style="blue",
        padding=(0, 2),
    ))
    console.print()

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=36),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    )

    with progress:
        # ── Stage 1: probe video ──────────────────────────────────────────
        t_meta = progress.add_task("[cyan]Probing video metadata …", total=None)
        info = probe_video(str(video_path))
        progress.update(
            t_meta,
            description=(
                f"[green]✓ {info['codec']} {info['width']}×{info['height']}"
                f" @ {info['fps']} fps  {_hms(info['duration'])}"
            ),
            total=1, completed=1,
        )

        # ── Stage 2: probe frames ─────────────────────────────────────────
        t_probe = progress.add_task("[cyan]Scanning frame types …  ", total=None)
        raw_frames = probe_frames(str(video_path))
        if not raw_frames:
            console.print("[red]ffprobe returned no video frames.[/]")
            sys.exit(1)

        gop = 0
        frames: list[dict] = []
        for idx, f in enumerate(raw_frames, start=1):
            if f["type"] == "I":
                gop += 1
            frames.append({"idx": idx, "type": f["type"], "pts": f["pts"], "gop": gop})

        i_n = sum(1 for f in frames if f["type"] == "I")
        p_n = sum(1 for f in frames if f["type"] == "P")
        b_n = sum(1 for f in frames if f["type"] == "B")
        progress.update(
            t_probe,
            description=(
                f"[green]✓ {len(frames)} frames"
                f"  I={i_n}  P={p_n}  B={b_n}  GOPs={gop}"
            ),
            total=1, completed=1,
        )

        # ── Stage 3: extract ──────────────────────────────────────────────
        t_extract = progress.add_task(
            f"[cyan]Extracting frames (w={args.width}px) …",
            total=len(frames),
        )
        extract_frames(str(video_path), frames_dir, args.width, len(frames), progress, t_extract)
        progress.update(t_extract, description="[green]✓ Frames extracted")

        # ── Stage 4: overlay ──────────────────────────────────────────────
        if not args.no_overlay:
            try:
                import PIL  # noqa: F401
                t_overlay = progress.add_task(
                    f"[cyan]Rendering overlays (×{args.workers} workers) …",
                    total=len(frames),
                )
                apply_overlays(frames, frames_dir, args.workers, progress, t_overlay)
                progress.update(t_overlay, description="[green]✓ Overlays applied")
            except ImportError:
                console.print("[yellow]Pillow not found — skipping overlays.[/]  "
                              "[dim]pip install Pillow[/]")

        # ── Stage 5: HTML ─────────────────────────────────────────────────
        t_html = progress.add_task("[cyan]Generating HTML …       ", total=None)
        generate_html(frames, info, html_path, "frames", video_path.name)
        progress.update(t_html, description="[green]✓ HTML written", total=1, completed=1)

    console.print()
    console.print(
        Columns([
            Panel(
                Text.assemble(
                    ("Frames\n", "dim"),
                    (f"{len(frames):,}", "bold white"), "\n",
                    ("I ", "red bold"), (f"{i_n:,}  ", "red"),
                    ("P ", "green bold"), (f"{p_n:,}  ", "green"),
                    ("B ", "blue bold"), (f"{b_n:,}", "blue"),
                ),
                border_style="dim",
            ),
            Panel(
                Text.assemble(
                    ("Output\n", "dim"),
                    (str(html_path.resolve()), "bold cyan"),
                ),
                border_style="dim",
            ),
        ]),
    )
    console.print()
    console.print(
        f"  [bold green]Open:[/] [cyan underline]file://{html_path.resolve()}[/]\n"
        f"  [dim]Tip: press [white]g[/] in the viewer to jump to a timestamp[/]"
    )
    console.print()


if __name__ == "__main__":
    main()
