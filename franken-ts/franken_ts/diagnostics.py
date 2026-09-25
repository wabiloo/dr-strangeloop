from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

from rich import box
from rich.table import Table
from rich.text import Text

from .pts import _load_idr_timestamps, PTS_CLOCK
from .config import is_segmentation_start_type_id
from .timeline import AdBoundary, TimelineEntry, pts_for_boundary

logger = logging.getLogger(__name__)

_FRAME_WARN = 2  # flag if drift > 2 frame-durations


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class DiagRow:
    label: str
    kind: str                             # ad_start | ad_stop | transition
    asset_t: float                        # calculated asset boundary (s)
    scte_target_t: Optional[float]        # pts_map value (s)
    scte_target_pts: Optional[int]        # pts_map value (ticks)
    actual_idr_t: Optional[float]         # nearest IDR in final TS (s)
    actual_scte_t: Optional[float]        # SCTE found in final TS (s)
    actual_scte_pts: Optional[int]        # SCTE found in final TS (ticks)
    event_id: Optional[int] = None        # ad event_id (None for transitions)


# ── Parse SCTE from tsduck verify XML ────────────────────────────────────────

@dataclass
class _ScteEntry:
    event_id: int
    is_start: bool       # out_of_network=true → start; false → stop
    pts_ticks: int
    pts_seconds: float


def _collect_scte_from_xml(xml_path: Path) -> dict[tuple[int, bool], _ScteEntry]:
    """Parse SCTE-35 PTS values from a tsduck-produced extraction XML.

    Handles both splice_insert and time_signal tables.  The XML is structured
    as a flat list of <splice_information_table> elements each containing
    exactly one command element (<splice_insert> or <time_signal>).

    For time_signal tables the PTS is on the <time_signal> child, NOT on the
    descriptor — so we must resolve it within the same table, not by scanning
    the whole document (which was the original bug: all descriptors picked up
    the first time_signal pts_time found anywhere).

    start vs stop for time_signal is determined from the explicit Table 23
    pairing map, with parity used only as a fallback for unknown legacy IDs.
    """
    if not xml_path.exists():
        return {}
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as exc:
        logger.warning("Cannot parse %s: %s", xml_path, exc)
        return {}

    def _parse_int(raw: str) -> int:
        raw = raw.replace(",", "").replace(" ", "")
        return int(raw, 16) if raw.startswith("0x") else int(raw)

    result: dict[tuple[int, bool], _ScteEntry] = {}

    for sit in root.iter("splice_information_table"):
        # ── splice_insert ─────────────────────────────────────────────────────
        si = sit.find("splice_insert")
        if si is not None:
            raw_id  = si.get("splice_event_id", "")
            raw_pts = si.get("pts_time", "")
            raw_oon = si.get("out_of_network", "").lower()
            try:
                event_id  = _parse_int(raw_id)
                pts_ticks = _parse_int(raw_pts)
                is_start  = (raw_oon == "true")
            except (ValueError, TypeError):
                continue
            key = (event_id, is_start)
            result[key] = _ScteEntry(event_id, is_start, pts_ticks, pts_ticks / PTS_CLOCK)
            continue

        # ── time_signal ───────────────────────────────────────────────────────
        ts = sit.find("time_signal")
        if ts is None:
            continue
        raw_pts = ts.get("pts_time", "")
        if not raw_pts:
            continue  # time_signal with no pts (e.g. immediate splice) — skip
        try:
            pts_ticks = _parse_int(raw_pts)
        except (ValueError, TypeError):
            continue

        for sd in sit.iter("splice_segmentation_descriptor"):
            raw_id   = sd.get("segmentation_event_id", "")
            raw_type = sd.get("segmentation_type_id", "0x00")
            try:
                event_id = _parse_int(raw_id)
                type_id  = _parse_int(raw_type)
            except (ValueError, TypeError):
                continue
            is_start = is_segmentation_start_type_id(type_id)
            key = (event_id, is_start)
            result[key] = _ScteEntry(event_id, is_start, pts_ticks, pts_ticks / PTS_CLOCK)

    return result


# ── Nearest helper ────────────────────────────────────────────────────────────

def _nearest(times: list[float], target: float) -> Optional[float]:
    if not times:
        return None
    return min(times, key=lambda t: abs(t - target))


# ── Build rows ────────────────────────────────────────────────────────────────

def build_diag_rows(
    entries: list[TimelineEntry],
    boundaries: list[AdBoundary],
    pts_map: dict[tuple[int, bool], int],
    final_ts: Path,
    verify_xml: Optional[Path],
    framerate: int,
    muxer_offset: float = 0.0,
) -> list[DiagRow]:
    """One row per marker boundary (start/stop, from `boundaries` -- covers
    any marker, single- or multi-asset, any nesting depth) plus one row per
    plain content transition (an entry boundary with no marker starting
    there). Rows are ordered by output time; multiple markers coincident at
    the same instant (e.g. a break ending exactly when its last ad ends)
    each get their own row, grouped by the HTML renderer below.
    """
    actual_idrs = _load_idr_timestamps(final_ts)
    actual_scte = _collect_scte_from_xml(verify_xml) if verify_xml else {}

    boundaries_by_time: dict[float, list[AdBoundary]] = {}
    for b in boundaries:
        boundaries_by_time.setdefault(b.output_time, []).append(b)

    # Plain content transitions: every entry start except the very first
    # (nothing before it) and any that coincide with a marker boundary
    # (that instant already gets an ad_start/ad_stop row below).
    transition_times = {
        e.output_start for e in entries[1:] if e.output_start not in boundaries_by_time
    }

    rows: list[DiagRow] = []
    for t in sorted(set(boundaries_by_time) | transition_times):
        actual_idr = _nearest(actual_idrs, t + muxer_offset)

        for b in boundaries_by_time.get(t, []):
            kind = "ad_start" if b.is_start else "ad_stop"
            label = f"Event #{b.event_id} {'Start' if b.is_start else 'Stop'}"
            scte_pts = pts_for_boundary(pts_map, b)
            scte_t = scte_pts / PTS_CLOCK if scte_pts else None
            actual_entry = actual_scte.get((b.event_id, b.is_start))

            rows.append(DiagRow(
                label=label,
                kind=kind,
                asset_t=t + muxer_offset,
                scte_target_t=scte_t,
                scte_target_pts=scte_pts,
                actual_idr_t=actual_idr,
                actual_scte_t=actual_entry.pts_seconds if actual_entry else None,
                actual_scte_pts=actual_entry.pts_ticks if actual_entry else None,
                event_id=b.event_id,
            ))

        if t in transition_times:
            entry = next((e for e in entries if e.output_start == t), None)
            if entry is not None:
                rows.append(DiagRow(
                    label=f"Transition → {entry.source_file.name}",
                    kind="transition",
                    asset_t=t + muxer_offset,
                    scte_target_t=None,
                    scte_target_pts=None,
                    actual_idr_t=actual_idr,
                    actual_scte_t=None,
                    actual_scte_pts=None,
                    event_id=None,
                ))

    return rows


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_s(t: Optional[float]) -> str:
    if t is None:
        return "—"
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def _delta_cell(ref: float, val: Optional[float], frame_dur: float) -> Text:
    if val is None:
        return Text("—", style="dim")
    txt = Text(_fmt_s(val))
    delta = val - ref
    if abs(delta) > frame_dur * _FRAME_WARN:
        sign = "+" if delta > 0 else ""
        txt.append(f"\n{sign}{delta:.3f}s", style="bold red")
    elif abs(delta) > 0.0005:
        sign = "+" if delta > 0 else ""
        txt.append(f"\n{sign}{delta:.3f}s", style="yellow")
    return txt


# ── Terminal table ────────────────────────────────────────────────────────────

def render_terminal_table(rows: list[DiagRow], framerate: int, muxer_offset: float = 0.0) -> Table:
    frame_dur = 1.0 / framerate
    offset_str = f"{muxer_offset:.6f}s" if muxer_offset else "0s"

    t = Table(
        title=f"Boundary Diagnostics  [dim](muxer PTS offset: {offset_str})[/dim]",
        box=box.SIMPLE_HEAD,
        show_lines=True,
        title_style="bold",
    )
    t.add_column("Boundary",                    style="bold", no_wrap=True)
    t.add_column("Expected IDR\n(bound+offset)", justify="right")
    t.add_column("SCTE XML target",             justify="right")
    t.add_column("SCTE target (PTS)",           justify="right", style="dim")
    t.add_column("Actual IDR (TS)",             justify="right")
    t.add_column("Actual SCTE (TS)",            justify="right")

    for row in rows:
        ref = row.asset_t  # already = boundary + muxer_offset
        fd = frame_dur
        t.add_row(
            row.label,
            _fmt_s(row.asset_t),
            _delta_cell(ref, row.scte_target_t, fd),
            f"{row.scte_target_pts:,}" if row.scte_target_pts else "—",
            _delta_cell(ref, row.actual_idr_t, fd),
            _delta_cell(ref, row.actual_scte_t, fd),
        )

    return t


# ── HTML table ────────────────────────────────────────────────────────────────

def render_html_table(rows: list[DiagRow], framerate: int, muxer_offset: float = 0.0) -> str:
    frame_dur = 1.0 / framerate

    def cell(ref: float, val: Optional[float]) -> str:
        if val is None:
            return '<td class="dim">—</td>'
        delta = val - ref
        delta_s = ""
        cls = "ok"
        if abs(delta) > frame_dur * _FRAME_WARN:
            sign = "+" if delta > 0 else ""
            delta_s = f'<br><span class="bad">{sign}{delta:.3f}s</span>'
            cls = "bad"
        elif abs(delta) > 0.0005:
            sign = "+" if delta > 0 else ""
            delta_s = f'<br><span class="warn">{sign}{delta:.3f}s</span>'
            cls = "warn"
        return f'<td class="{cls}">{_fmt_s(val)}{delta_s}</td>'

    def row_html(row: DiagRow, extra_cls: str = "") -> str:
        kind_cls = {"ad_start": "ad-start", "ad_stop": "ad-stop", "transition": "trans"}.get(row.kind, "")
        cls = f"{kind_cls} {extra_cls}".strip()
        pts_str = f"{row.scte_target_pts:,}" if row.scte_target_pts else "—"
        return (
            f'<tr class="{cls}">'
            f'<td class="label">{row.label}</td>'
            f'<td class="ref">{_fmt_s(row.asset_t)}</td>'
            + cell(row.asset_t, row.scte_target_t)
            + f'<td class="dim">{pts_str}</td>'
            + cell(row.asset_t, row.actual_idr_t)
            + cell(row.asset_t, row.actual_scte_t)
            + "</tr>"
        )

    rows_html = []
    i = 0
    while i < len(rows):
        row = rows[i]
        next_row = rows[i + 1] if i + 1 < len(rows) else None

        # Detect a paired ad_start / ad_stop for the same event_id
        is_paired_start = (
            row.kind == "ad_start"
            and next_row is not None
            and next_row.kind == "ad_stop"
            and row.event_id is not None
            and row.event_id == next_row.event_id
        )

        if is_paired_start:
            stop_row = next_row
            rows_html.append(
                f'<tr class="group-hdr">'
                f'<td colspan="6">'
                f'<span class="group-event-label">Event #{row.event_id}</span>'
                f'</td></tr>'
            )
            rows_html.append(row_html(row, "group-member group-start"))
            rows_html.append(row_html(stop_row, "group-member group-end"))
            rows_html.append('<tr class="group-sep"><td colspan="6"></td></tr>')
            i += 2
        else:
            rows_html.append(row_html(row))
            i += 1

    offset_note = f" &nbsp;·&nbsp; muxer PTS offset: <code>{muxer_offset:.6f}s</code>" if muxer_offset else ""

    return f"""
<div class="diag-wrap">
  <h2>Boundary Diagnostics</h2>
  <table class="diag">
    <thead>
      <tr>
        <th>Boundary</th>
        <th>Expected IDR<br>(boundary + offset)</th>
        <th>SCTE XML target</th>
        <th>SCTE target (PTS)</th>
        <th>Actual IDR in TS</th>
        <th>Actual SCTE in TS</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows_html)}
    </tbody>
  </table>
  <p class="diag-note">Deltas shown relative to expected IDR position (asset boundary + muxer offset).{offset_note}<br><span class="bad">Red</span> = &gt;{_FRAME_WARN} frames drift. <span class="warn">Yellow</span> = minor drift.</p>
</div>"""
