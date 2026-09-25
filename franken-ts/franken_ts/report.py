from __future__ import annotations

import base64
import datetime
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from .config import MarkerConfig
from .timeline import AdBoundary, TimelineEntry, pts_for_boundary

logger = logging.getLogger(__name__)

THUMB_W = 160
THUMB_H = 90


# ── Data model ──────────────────────────────────────────────────────────────

@dataclass
class BoundaryPoint:
    label: str
    kind: Literal["ad_start", "ad_stop", "transition"]
    timestamp: float          # seconds in the output TS
    pts_ticks: Optional[int]  # 90 kHz ticks, if known from pts_map
    event_id: Optional[int]
    marker: Optional[MarkerConfig] = None      # config for ad events
    break_duration: Optional[float] = None     # marker span length in seconds
    marker_index: Optional[int] = None


def _collect_boundary_points(
    entries: list[TimelineEntry],
    boundaries: list[AdBoundary],
    pts_map: dict[tuple[int, bool], int],
) -> list[BoundaryPoint]:
    """One point per marker boundary (any span, any nesting depth) plus one
    per plain content transition not coincident with a marker boundary."""
    points: list[BoundaryPoint] = []

    boundaries_by_time: dict[float, list[AdBoundary]] = {}
    for b in boundaries:
        boundaries_by_time.setdefault(b.output_time, []).append(b)

    for b in boundaries:
        pts = pts_for_boundary(pts_map, b)
        ts = pts / 90_000 if pts else b.output_time
        points.append(BoundaryPoint(
            label=f"Event #{b.event_id} {'Start' if b.is_start else 'Stop'}",
            kind="ad_start" if b.is_start else "ad_stop",
            timestamp=ts,
            pts_ticks=pts,
            event_id=b.event_id,
            marker=b.marker,
            break_duration=b.break_duration,
            marker_index=b.marker_index,
        ))

    for i, entry in enumerate(entries):
        if i == 0:
            continue  # nothing before the first asset
        if entry.output_start in boundaries_by_time:
            continue  # already represented by an ad_start/ad_stop point above
        points.append(BoundaryPoint(
            label=f"Content Transition ({entry.source_file.name})",
            kind="transition",
            timestamp=entry.output_start,
            pts_ticks=None,
            event_id=None,
        ))

    points.sort(key=lambda p: p.timestamp)
    return points


# ── Frame extraction ─────────────────────────────────────────────────────────

def _extract_frame(ts_file: Path, timestamp: float) -> Optional[bytes]:
    """Extract a single JPEG thumbnail with frame-accurate seeking.

    Uses a two-pass seek: a fast container seek to 2s before the target
    (landing on a nearby keyframe), then an output -ss at the absolute
    target PTS to decode-forward and discard frames until the target.

    -copyts is REQUIRED here.  Without it, the input -ss resets the
    decoded stream's timestamps to ~0, so the second (output) -ss would be
    applied relative to the seek point — the effective seek becomes
    pre_seek + target (≈ 2·target − 2) and the wrong frame is grabbed.
    -copyts preserves the original PTS so the output -ss target lands on
    the exact frame whose PTS == target.
    """
    ts = max(0.0, timestamp)
    pre_seek = max(0.0, ts - 2.0)

    result = subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-copyts",                  # keep original PTS so output -ss is absolute
            "-ss", f"{pre_seek:.6f}",   # fast seek to nearby keyframe
            "-i", str(ts_file),
            "-ss", f"{ts:.6f}",         # output: discard until absolute target PTS
            "-frames:v", "1",
            "-vf", f"scale={THUMB_W}:{THUMB_H}:flags=lanczos",
            "-f", "image2",
            "-vcodec", "mjpeg",
            "-q:v", "5",
            "pipe:1",
        ],
        capture_output=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 and result.stdout else None


def _img_tag(data: Optional[bytes], label: str, highlight: bool = False) -> str:
    if data:
        b64 = base64.b64encode(data).decode()
        src = f"data:image/jpeg;base64,{b64}"
    else:
        # 1×1 transparent GIF placeholder
        src = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"

    cls = "frame highlight" if highlight else "frame"
    return (
        f'<div class="{cls}">'
        f'<img src="{src}" width="{THUMB_W}" height="{THUMB_H}" loading="lazy" alt="{label}">'
        f'<div class="frame-label">{label}</div>'
        f'</div>'
    )


# ── HTML helpers ─────────────────────────────────────────────────────────────

def _fmt_time(seconds: float) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def _marker_intervals(boundaries: list[AdBoundary]) -> list[tuple[float, float]]:
    """Pair up start/stop boundaries by internal marker identity,
    covering markers of any span/depth -- used to shade the timeline bar."""
    starts = {b.marker_index: b.output_time for b in boundaries if b.is_start}
    stops = {b.marker_index: b.output_time for b in boundaries if not b.is_start}
    return [(starts[index], stops[index]) for index in starts if index in stops]


def _build_timeline_bar(entries: list[TimelineEntry], total: float, boundaries: list[AdBoundary]) -> str:
    if total == 0:
        return ""
    intervals = _marker_intervals(boundaries)
    segs = []
    for entry in entries:
        pct_l = entry.output_start / total * 100
        pct_w = (entry.output_end - entry.output_start) / total * 100
        is_ad = any(lo <= entry.output_start and entry.output_end <= hi for lo, hi in intervals)
        kind = "ad" if is_ad else "content"
        title = (
            f"{entry.source_file.name} "
            f"[{_fmt_time(entry.output_start)} → {_fmt_time(entry.output_end)}]"
        )
        segs.append(
            f'<div class="seg {kind}" '
            f'style="left:{pct_l:.4f}%;width:{pct_w:.4f}%" '
            f'title="{title}"></div>'
        )
    return f'<div class="tl-bar">{"".join(segs)}</div>'


def _build_boundary_section(
    point: BoundaryPoint,
    ts_file: Path,
    total_duration: float,
    frame_dur: float,
) -> str:
    t = point.timestamp
    frame_specs = [
        (t - 3 * frame_dur, "−3"),
        (t - 2 * frame_dur, "−2"),
        (t - 1 * frame_dur, "−1"),
        (t,                  "+0"),
        (t + 1 * frame_dur,  "+1"),
        (t + 2 * frame_dur,  "+2"),
    ]

    frames_html: list[str] = []
    for idx, (ft, label) in enumerate(frame_specs):
        if idx == 3:  # insert splice line between −1 and +0
            frames_html.append(f'<div class="splice-line {point.kind}"></div>')

        if 0 <= ft <= total_duration:
            logger.debug("  extracting frame %s at %.3fs", label, ft)
            data = _extract_frame(ts_file, ft)
        else:
            data = None
        frames_html.append(_img_tag(data, label, highlight=(label == "+0")))

    pts_str = f"PTS {point.pts_ticks:,}" if point.pts_ticks else f"≈ {point.timestamp:.3f}s"
    label_upper = point.label.upper()
    scte_html = _build_scte_panel(point)

    return f"""
    <section class="boundary {point.kind}">
      <div class="bh">
        <span class="tag {point.kind}">{label_upper}</span>
        <span class="ts">{_fmt_time(point.timestamp)}</span>
        <span class="pts">{pts_str}</span>
      </div>
      <div class="frames">{"".join(frames_html)}</div>
      {scte_html}
    </section>"""


def _build_scte_panel(point: BoundaryPoint) -> str:
    """Render a compact metadata strip showing the SCTE-35 marker fields."""
    ab = point.marker
    if not ab:
        return ""

    is_start = (point.kind == "ad_start")

    def field(key: str, val: str, cls: str = "") -> str:
        val_cls = f"scte-val {cls}".strip()
        return (
            f'<div class="scte-field">'
            f'<span class="scte-key">{key}</span>'
            f'<span class="{val_cls}">{val}</span>'
            f'</div>'
        )

    parts: list[str] = []
    parts.append(field("event_id", f"0x{ab.event_id:08X}"))

    if point.pts_ticks:
        parts.append(field(
            "pts_time",
            f"{point.pts_ticks:,} &nbsp;({_fmt_time(point.pts_ticks / 90_000)})",
            "pts",
        ))

    if ab.splice_type == "splice_insert":
        parts.append(field("out_of_network", "true" if is_start else "false"))
        if is_start and point.break_duration:
            dur_pts = round(point.break_duration * 90_000)
            parts.append(field("break_duration", f"{dur_pts:,} &nbsp;({point.break_duration:.3f}s)"))
        parts.append(field("unique_program_id", ab.unique_program_id))
        parts.append(field("avail", f"{ab.avail_num} / {ab.avails_expected}"))
        parts.append(field("provider_avail_id", ab.provider_avail_id))

    elif ab.splice_type == "time_signal" and ab.segmentation:
        seg = ab.segmentation
        parts.append(field("segmentation_type_id", seg.type_id))
        if is_start and point.break_duration:
            dur_pts = round(point.break_duration * 90_000)
            parts.append(field("segmentation_duration", f"{dur_pts:,} &nbsp;({point.break_duration:.3f}s)"))
        if seg.upid_hex:
            parts.append(field("upid", f"type={seg.upid_type} · {seg.upid_hex}"))
        flags = []
        if seg.web_delivery_allowed:
            flags.append("web_delivery")
        if seg.no_regional_blackout:
            flags.append("no_blackout")
        if seg.archive_allowed:
            flags.append("archive")
        if flags:
            parts.append(field("flags", " · ".join(flags)))

    return f'<div class="scte-panel">{"".join(parts)}</div>'


# ── Event-group rendering ─────────────────────────────────────────────────────

def _build_event_group(
    event_id: int,
    start_p: Optional[BoundaryPoint],
    stop_p: Optional[BoundaryPoint],
    ts_file: Path,
    total_duration: float,
    frame_dur: float,
) -> str:
    """Wrap a paired start + stop boundary into a single visual group card."""
    ab = (start_p or stop_p).marker if (start_p or stop_p) else None  # type: ignore[union-attr]
    splice_type = ab.splice_type if ab else "unknown"
    break_dur = (start_p or stop_p).break_duration if (start_p or stop_p) else None  # type: ignore[union-attr]

    dur_str = ""
    if break_dur is not None:
        h, rem = divmod(break_dur, 3600)
        m, s = divmod(rem, 60)
        dur_str = f"{int(h):02d}:{int(m):02d}:{s:06.3f}"

    type_badge = f'<span class="scte-type-badge {splice_type}">{splice_type}</span>'
    dur_span = f'<span class="egdur">{dur_str}</span>' if dur_str else ""

    hdr = (
        f'<div class="event-group-hdr">'
        f'<span class="egid">Ad Event #{event_id}</span>'
        f'{type_badge}'
        f'{dur_span}'
        f'</div>'
    )

    start_html = _build_boundary_section(start_p, ts_file, total_duration, frame_dur) if start_p else ""
    stop_html = _build_boundary_section(stop_p, ts_file, total_duration, frame_dur) if stop_p else ""

    connector = ""
    if start_p and stop_p and dur_str:
        connector = (
            f'<div class="break-connector">'
            f'<span class="break-connector-line"></span>'
            f'<span class="break-dur-label">{dur_str} ad break</span>'
            f'<span class="break-connector-line"></span>'
            f'</div>'
        )

    return f'<div class="event-group">{hdr}{start_html}{connector}{stop_html}</div>'


def _render_all_sections(
    points: list[BoundaryPoint],
    ts_file: Path,
    total_duration: float,
    frame_dur: float,
) -> str:
    """Render boundary sections, grouping paired ad events into a shared card."""
    event_points: dict[int, list[BoundaryPoint]] = {}
    order: list[tuple[str, object]] = []

    for p in points:
        if p.event_id is not None:
            eid = p.event_id
            if eid not in event_points:
                event_points[eid] = []
                order.append(("event", eid))
            event_points[eid].append(p)
        else:
            order.append(("transition", p))

    parts: list[str] = []
    for item_type, item in order:
        if item_type == "event":
            eid = int(item)  # type: ignore[arg-type]
            ps = event_points[eid]
            start_p = next((p for p in ps if p.kind == "ad_start"), None)
            stop_p = next((p for p in ps if p.kind == "ad_stop"), None)
            parts.append(_build_event_group(eid, start_p, stop_p, ts_file, total_duration, frame_dur))
        else:
            parts.append(_build_boundary_section(item, ts_file, total_duration, frame_dur))  # type: ignore[arg-type]

    return "\n".join(parts)



def generate_report(
    ts_file: Path,
    entries: list[TimelineEntry],
    boundaries: list[AdBoundary],
    pts_map: dict[tuple[int, bool], int],
    framerate: int,
    output_path: Path,
    diag_html: str = "",
    dry_run: bool = False,
) -> None:
    if dry_run:
        logger.info("[dry-run] Would write report to %s", output_path)
        return

    total = entries[-1].output_end if entries else 0.0
    frame_dur = 1.0 / framerate
    points = _collect_boundary_points(entries, boundaries, pts_map)
    event_count = len({b.event_id for b in boundaries})

    logger.info("Building report: %d boundary points, total %.1fs", len(points), total)

    timeline_html = _build_timeline_bar(entries, total, boundaries)
    sections_html = _render_all_sections(points, ts_file, total, frame_dur)

    html = _render_page(
        filename=ts_file.name,
        total=total,
        event_count=event_count,
        timeline_html=timeline_html,
        diag_html=diag_html,
        sections_html=sections_html,
    )
    output_path.write_text(html, encoding="utf-8")
    logger.info("Report written to %s", output_path)


def _render_page(
    filename: str,
    total: float,
    event_count: int,
    timeline_html: str,
    sections_html: str,
    diag_html: str = "",
) -> str:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tw, th = THUMB_W, THUMB_H

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>🧟 franken-ts — {filename}</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0c0c14;color:#c4c4d4;font-family:"JetBrains Mono","Fira Code","SF Mono",monospace;font-size:13px;line-height:1.5;padding:28px}}
header{{display:flex;align-items:baseline;gap:20px;border-bottom:1px solid #252536;padding-bottom:16px;margin-bottom:28px}}
h1{{font-size:20px;color:#e8e8f4;letter-spacing:-.5px}}
.meta{{color:#55556a;font-size:12px}}
.meta span{{margin-right:18px}}
h2{{font-size:10px;text-transform:uppercase;letter-spacing:1.2px;color:#44445a;margin-bottom:10px}}
.tl-wrap{{margin-bottom:36px}}
.tl-bar{{position:relative;height:30px;background:#13131f;border-radius:4px;overflow:hidden;border:1px solid #232336}}
.seg{{position:absolute;top:0;height:100%}}
.seg:hover{{filter:brightness(1.25);cursor:default}}
.seg.content{{background:#15345a;border-right:1px solid #0c0c14}}
.seg.ad{{background:#4d1e00;border-right:1px solid #0c0c14}}
.boundaries{{display:flex;flex-direction:column;gap:28px}}
.boundary{{border:1px solid #1e1e30;border-radius:6px;overflow:hidden;background:#0f0f1a}}
.bh{{display:flex;align-items:center;gap:16px;padding:10px 16px;background:#141424;border-bottom:1px solid #1e1e30}}
.tag{{font-size:10px;font-weight:700;letter-spacing:.9px;padding:3px 8px;border-radius:3px}}
.tag.ad_start{{background:#5c2400;color:#ffaa66}}
.tag.ad_stop{{background:#0b3318;color:#66ffaa}}
.tag.transition{{background:#172260;color:#7799ff}}
.ts{{color:#7a7a9a}}
.pts{{color:#44445a;font-size:11px}}
.frames{{display:flex;align-items:center;gap:4px;padding:14px 16px;overflow-x:auto}}
.frame{{display:flex;flex-direction:column;align-items:center;gap:5px;flex-shrink:0}}
.frame img{{display:block;border-radius:3px;border:2px solid transparent;opacity:.65}}
.frame.highlight img{{opacity:1;border-color:#e06000}}
.frame-label{{font-size:10px;color:#44445a;text-align:center}}
.splice-line{{flex-shrink:0;width:3px;height:{th}px;border-radius:2px;margin:0 8px;align-self:center}}
.splice-line.ad_start{{background:linear-gradient(#ff6b00,#cc3300)}}
.splice-line.ad_stop{{background:linear-gradient(#00c853,#007a30)}}
.splice-line.transition{{background:linear-gradient(#4488ff,#1144dd)}}
/* event groups */
.event-group{{border:1px solid #1e1e30;border-radius:8px;overflow:hidden}}
.event-group .boundary{{border:none;border-radius:0;background:transparent}}
.event-group .boundary+.boundary{{border-top:1px solid #1a1a28}}
.event-group-hdr{{display:flex;align-items:center;gap:12px;padding:7px 16px;background:#0d0d1e;border-bottom:1px solid #1e1e30}}
.egid{{font-size:9px;font-weight:700;letter-spacing:1.2px;color:#5555aa;text-transform:uppercase}}
.egdur{{font-size:11px;color:#33334a;margin-left:auto}}
.scte-type-badge{{font-size:9px;font-weight:700;letter-spacing:.8px;padding:2px 7px;border-radius:3px;text-transform:uppercase}}
.scte-type-badge.splice_insert{{background:#162016;color:#55bb55}}
.scte-type-badge.time_signal{{background:#141428;color:#5577cc}}
.break-connector{{display:flex;align-items:center;gap:12px;padding:5px 20px;background:#0a0a12;border-top:1px solid #141420;border-bottom:1px solid #141420}}
.break-connector-line{{flex:1;height:1px;background:#1a1a28}}
.break-dur-label{{font-size:10px;color:#2e2e48;letter-spacing:.5px;white-space:nowrap}}
/* scte panel */
.scte-panel{{display:flex;flex-wrap:wrap;gap:4px 22px;padding:8px 16px 10px;background:#0b0b16;border-top:1px dashed #181828}}
.scte-field{{display:flex;gap:6px;align-items:baseline;font-size:11px}}
.scte-key{{color:#2e2e4a}}
.scte-val{{color:#6666aa}}
.scte-val.pts{{color:#44447a}}
/* diagnostics table */
.diag-wrap{{margin-bottom:36px}}
.diag-wrap h2{{margin-bottom:12px}}
.diag{{width:100%;border-collapse:collapse;font-size:12px}}
.diag th{{background:#161626;color:#7777aa;font-weight:600;letter-spacing:.5px;padding:6px 12px;text-align:left;border-bottom:2px solid #2a2a3d}}
.diag td{{padding:6px 12px;border-bottom:1px solid #1a1a28;vertical-align:top;line-height:1.4}}
.diag tr:hover td{{background:#0f0f20}}
.diag .ref{{color:#8888aa}}
.diag .dim{{color:#44445a}}
.diag .ok{{color:#c4c4d4}}
.diag .bad{{color:#ff6060}}
.diag .warn{{color:#ffbb44}}
.diag .label{{color:#e8e8f4;font-weight:600}}
.diag .ad-start .label::before{{content:"▶ ";color:#ff8844}}
.diag .ad-stop .label::before{{content:"◀ ";color:#44cc88}}
.diag .trans .label::before{{content:"⇄ ";color:#4488ff}}
.diag .group-hdr td{{background:#0d0d1c;padding:3px 12px;border-top:2px solid #1e1e30;border-bottom:none}}
.diag .group-hdr .group-event-label{{font-size:9px;text-transform:uppercase;letter-spacing:1.2px;color:#33334a}}
.diag .group-member td{{background:#0e0e1d}}
.diag .group-member:hover td{{background:#10102a}}
.diag .group-start td:first-child{{border-left:3px solid #5c2400}}
.diag .group-end td:first-child{{border-left:3px solid #0b3318}}
.diag .group-sep td{{padding:2px 0;border-bottom:2px solid #1e1e30}}
.diag-note{{margin-top:8px;font-size:11px;color:#44445a}}
</style>
</head>
<body>
<header>
  <h1>🧟 franken-ts</h1>
  <div class="meta">
    <span>📄 {filename}</span>
    <span>⏱ {_fmt_time(total)}</span>
    <span>🎯 {event_count} ad event(s)</span>
    <span>🕐 {now}</span>
  </div>
</header>
<div class="tl-wrap">
  <h2>Timeline</h2>
  {timeline_html}
</div>
{diag_html}
<div class="boundaries">
{sections_html}
</div>
</body>
</html>"""
