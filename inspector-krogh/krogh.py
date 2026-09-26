#!/usr/bin/env python3
"""
krogh.py

Scans a built ``.ts`` file for its *actual* SCTE-35 markers -- completely
independently of franken-ts (no playlist, no ``.markers.json`` sidecar, no
shared build state) -- and produces a JSON report plus (optionally) an HTML
viewer with frames extracted around every splice boundary.

This is the tool you reach for to answer "does this file itself actually
carry the markers it's supposed to, in the right place" -- as opposed to
franken-ts's own ``--verify``/``--report-only``, which checks the file
against the playlist franken-ts itself built it from.

Requirements
------------
  ffmpeg + ffprobe   (frame extraction, video/keyframe probing)
  tsduck (`tsp`)     (SCTE-35 table extraction)

Usage
-----
  uv run krogh outputs/my_stream.ts
  uv run krogh outputs/my_stream.ts --output outputs/my_stream_scte
  uv run krogh outputs/my_stream.ts --skip-frames   # metadata only
  uv run krogh --render-only outputs/my_stream_scte/scte-report.json
  uv run krogh outputs/my_stream.ts --expected outputs/my_stream.markers.json   # (auto-discovered if present)
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
)

import scte35_tables as tables
from krogh_expected import compare_expected, discover_expected_path, load_expected
from scte35_filmstrip_html import render_filmstrip
from scte35_report_html import render_html

logger = logging.getLogger(__name__)
console = Console()

PTS_CLOCK = 90_000
REPORT_VERSION = 1
ENDCAP_FRAMES = 3
SCTE35_TABLE_ID = "0xFC"  # splice_information_table, per ANSI/SCTE 35


# ── data model ────────────────────────────────────────────────────────────────

@dataclass
class RawEvent:
    event_id: int
    splice_type: str                       # splice_insert | time_signal
    is_start: bool
    is_instant: bool
    pts_ticks: int
    pts_seconds: float
    segmentation_type_id: Optional[int] = None
    segmentation_duration_ticks: Optional[int] = None
    segment_num: Optional[int] = None
    segments_expected: Optional[int] = None
    upid_type: Optional[str] = None
    upid_hex: Optional[str] = None
    flags: dict = field(default_factory=dict)


@dataclass
class Marker:
    event_id: int
    splice_type: str
    segmentation_type_id: Optional[int]
    is_instant: bool
    start: Optional[RawEvent] = None
    stop: Optional[RawEvent] = None
    nesting_depth: Optional[int] = None
    contains: list[int] = field(default_factory=list)

    @property
    def type_label(self) -> str:
        if self.segmentation_type_id is None:
            return "Splice Insert"
        return tables.type_name(self.segmentation_type_id).replace(" Start", "").replace(" End", "")

    @property
    def type_code(self) -> str:
        if self.segmentation_type_id is None:
            return "SPI"
        return tables.type_code(self.segmentation_type_id)

    @property
    def start_seconds(self) -> Optional[float]:
        return self.start.pts_seconds if self.start else None

    @property
    def end_seconds(self) -> Optional[float]:
        return self.stop.pts_seconds if self.stop else None

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.start and self.stop:
            return self.stop.pts_seconds - self.start.pts_seconds
        return None


# ── tsduck extraction ────────────────────────────────────────────────────────

def run_tsduck_extract(ts_path: Path, xml_out: Path, pid: Optional[str] = None) -> None:
    """Run tsduck's `tables` plugin, filtering by SCTE-35 table id (0xFC) so
    the scan works regardless of which PID a given `.ts` happens to carry
    SCTE-35 on -- pass `pid` to additionally restrict to a known PID."""
    cmd = ["tsp", "-I", "file", str(ts_path), "-P", "tables", "--tid", SCTE35_TABLE_ID]
    if pid:
        cmd += ["--pid", pid]
    cmd += ["--xml", str(xml_out), "-O", "drop"]

    logger.info("Running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        console.print(f"[bold red]tsduck extraction failed:[/]\n{proc.stderr.strip()}")
        sys.exit(1)


def _parse_int(raw: str) -> int:
    raw = raw.strip().replace(",", "").replace(" ", "")
    return int(raw, 16) if raw.lower().startswith("0x") else int(raw)


def parse_scte35_xml(xml_path: Path) -> list[RawEvent]:
    """Parse every splice_insert / time_signal(+segmentation descriptors)
    table found in a tsduck `--xml` dump into flat RawEvent records.

    A single time_signal table may carry *multiple*
    splice_segmentation_descriptor children sharing one pts_time -- that's
    exactly how concurrent segmentation types (e.g. a Break and a Provider
    Ad Block starting at the same instant) are signalled on the wire, so
    each descriptor becomes its own event here rather than collapsing them.
    """
    if not xml_path.exists():
        return []
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as exc:
        logger.warning("Cannot parse %s: %s", xml_path, exc)
        return []

    events: list[RawEvent] = []

    for sit in root.iter("splice_information_table"):
        si = sit.find("splice_insert")
        if si is not None:
            try:
                event_id = _parse_int(si.get("splice_event_id", ""))
                pts_ticks = _parse_int(si.get("pts_time", ""))
                is_start = si.get("out_of_network", "").lower() == "true"
            except (ValueError, TypeError):
                continue
            events.append(RawEvent(
                event_id=event_id,
                splice_type="splice_insert",
                is_start=is_start,
                is_instant=False,
                pts_ticks=pts_ticks,
                pts_seconds=pts_ticks / PTS_CLOCK,
            ))
            continue

        ts_cmd = sit.find("time_signal")
        if ts_cmd is None:
            continue
        raw_pts = ts_cmd.get("pts_time", "")
        if not raw_pts:
            continue  # immediate signal with no PTS -- can't place it on a timeline
        try:
            pts_ticks = _parse_int(raw_pts)
        except (ValueError, TypeError):
            continue

        for sd in sit.iter("splice_segmentation_descriptor"):
            try:
                event_id = _parse_int(sd.get("segmentation_event_id", ""))
                type_id = _parse_int(sd.get("segmentation_type_id", "0x00"))
            except (ValueError, TypeError):
                continue

            is_instant = tables.is_instant_type_id(type_id)
            is_start = tables.is_start_type_id(type_id)

            dur_attr = sd.get("segmentation_duration")
            seg_num = sd.get("segment_num")
            segs_exp = sd.get("segments_expected")

            upid_type = None
            upid_hex = None
            upid_el = sd.find("segmentation_upid")
            if upid_el is not None:
                upid_type = upid_el.get("type")
                upid_hex = (upid_el.text or "").strip() or upid_el.get("value")

            flags: dict = {
                k: sd.get(k, "").lower() == "true"
                for k in ("web_delivery_allowed", "no_regional_blackout", "archive_allowed")
                if sd.get(k) is not None
            }
            if sd.get("device_restrictions") is not None:
                try:
                    flags["device_restrictions"] = _parse_int(sd.get("device_restrictions"))
                except (ValueError, TypeError):
                    pass

            events.append(RawEvent(
                event_id=event_id,
                splice_type="time_signal",
                is_start=is_start,
                is_instant=is_instant,
                pts_ticks=pts_ticks,
                pts_seconds=pts_ticks / PTS_CLOCK,
                segmentation_type_id=type_id,
                segmentation_duration_ticks=_parse_int(dur_attr) if dur_attr else None,
                segment_num=int(seg_num) if seg_num is not None else None,
                segments_expected=int(segs_exp) if segs_exp is not None else None,
                upid_type=upid_type,
                upid_hex=upid_hex,
                flags=flags,
            ))

    events.sort(key=lambda e: e.pts_seconds)
    return events


# ── pairing + nesting ──────────────────────────────────────────────────────────

def pair_events_into_markers(events: list[RawEvent]) -> list[Marker]:
    """Group flat events into start/stop (or standalone/instant) markers.

    Grouped by (event_id, splice_type, segmentation_type_id-pair) rather
    than bare event_id alone, since two unrelated signals could reuse an
    event_id in relaxed/legacy streams -- pairing only a start with the
    Table 23 end type_id it actually expects avoids mismatching them.
    """
    markers: list[Marker] = []
    consumed: set[int] = set()

    by_id: dict[int, list[RawEvent]] = {}
    for e in events:
        by_id.setdefault(e.event_id, []).append(e)

    for event_id, evs in by_id.items():
        for e in evs:
            if id(e) in consumed:
                continue
            if e.is_instant:
                markers.append(Marker(
                    event_id=event_id, splice_type=e.splice_type,
                    segmentation_type_id=e.segmentation_type_id,
                    is_instant=True, start=e,
                ))
                consumed.add(id(e))
                continue

            if e.is_start:
                expected_end = (
                    tables.SEGMENTATION_END_TYPE_ID.get(e.segmentation_type_id)
                    if e.segmentation_type_id is not None else None
                )
                stop = next(
                    (o for o in evs if id(o) not in consumed and not o.is_start
                     and (expected_end is None or o.segmentation_type_id == expected_end)),
                    None,
                )
                markers.append(Marker(
                    event_id=event_id, splice_type=e.splice_type,
                    segmentation_type_id=e.segmentation_type_id,
                    is_instant=False, start=e, stop=stop,
                ))
                consumed.add(id(e))
                if stop is not None:
                    consumed.add(id(stop))
            else:
                # A stop with no matching start earlier in the file (e.g.
                # the file was trimmed mid-break) -- still worth reporting.
                markers.append(Marker(
                    event_id=event_id, splice_type=e.splice_type,
                    segmentation_type_id=e.segmentation_type_id,
                    is_instant=False, stop=e,
                ))
                consumed.add(id(e))

    markers.sort(key=lambda m: (m.start_seconds if m.start_seconds is not None else m.end_seconds or 0.0))
    return markers


def infer_nesting(markers: list[Marker]) -> None:
    """Compute nesting depth/containment purely from PTS-span overlap
    across whatever markers were actually found in the file -- this is how
    concurrent segmentation types (e.g. a Break containing a PPO containing
    an Ad) show up on the wire even with zero playlist context."""
    spans = [m for m in markers if m.start_seconds is not None and m.end_seconds is not None]

    def contains(a: Marker, b: Marker) -> bool:
        if a is b:
            return False
        return a.start_seconds <= b.start_seconds and b.end_seconds <= a.end_seconds and (
            a.start_seconds, a.end_seconds
        ) != (b.start_seconds, b.end_seconds)

    for m in spans:
        m.nesting_depth = sum(1 for other in spans if contains(other, m))

    for m in spans:
        children = [o for o in spans if contains(m, o)]
        # Keep only *immediate* children: drop any child that's also
        # contained by another child of m (i.e. nested one level deeper).
        immediate = [
            c for c in children
            if not any(contains(other, c) for other in children if other is not c)
        ]
        m.contains = sorted(c.event_id for c in immediate)


# ── frame + IDR probing ────────────────────────────────────────────────────────

def probe_video_info(ts_path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_streams", "-show_format", "-of", "json", str(ts_path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        console.print(f"[bold red]ffprobe failed:[/]\n{proc.stderr.strip()}")
        sys.exit(1)
    data = json.loads(proc.stdout)
    s = data.get("streams", [{}])[0]
    f = data.get("format", {})
    try:
        n, d = s.get("r_frame_rate", "0/1").split("/")
        fps = round(int(n) / int(d), 4) if int(d) else 0.0
    except Exception:
        fps = 0.0
    return {
        "codec": s.get("codec_name", "?"),
        "profile": s.get("profile", ""),
        "width": s.get("width", 0),
        "height": s.get("height", 0),
        "fps": fps,
        "duration": float(f.get("duration") or s.get("duration") or 0),
    }


def load_frame_times(ts_path: Path) -> tuple[list[float], list[float]]:
    """Return (IDR/key-frame timestamps, all frame timestamps), both sorted."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames",
         "-show_entries", "frame=key_frame,best_effort_timestamp_time",
         "-of", "csv", str(ts_path)],
        capture_output=True, text=True,
    )
    idr: list[float] = []
    every: list[float] = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 3:
            try:
                t = float(parts[2])
            except ValueError:
                continue
            every.append(t)
            if parts[1] == "1":
                idr.append(t)
    idr.sort()
    every.sort()
    return idr, every


def nearest(times: list[float], target: float) -> Optional[float]:
    if not times:
        return None
    return min(times, key=lambda t: abs(t - target))


def extract_frame(ts_path: Path, timestamp: float, width: int) -> Optional[bytes]:
    """Two-pass seek: fast container seek near the target, then an exact
    -copyts output seek so the grabbed frame's PTS matches `timestamp`."""
    ts = max(0.0, timestamp)
    pre_seek = max(0.0, ts - 2.0)
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-copyts",
         "-ss", f"{pre_seek:.6f}", "-i", str(ts_path),
         "-ss", f"{ts:.6f}", "-frames:v", "1",
         "-vf", f"scale={width}:-2:flags=lanczos",
         "-f", "image2", "-vcodec", "mjpeg", "-q:v", "4", "pipe:1"],
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 and result.stdout else None


# ── checks ─────────────────────────────────────────────────────────────────────

def build_checks(markers: list[Marker], idr_times: list[float], frame_dur: float,
                  idr_tolerance_frames: float, duration_tolerance_frames: float) -> list[dict]:
    checks: list[dict] = []
    idr_tol = idr_tolerance_frames * frame_dur
    dur_tol = duration_tolerance_frames * frame_dur

    for m in markers:
        for boundary, ev in (("start", m.start), ("stop", m.stop)):
            if ev is None:
                continue
            idr = nearest(idr_times, ev.pts_seconds)
            drift = abs(idr - ev.pts_seconds) if idr is not None else None
            passed = idr is not None and drift <= idr_tol
            checks.append({
                "event_id": m.event_id,
                "boundary": boundary,
                "check": "lands_on_idr",
                "pass": passed,
                "detail": (
                    f"nearest IDR {drift:.3f}s away (tolerance {idr_tol:.3f}s)" if idr is not None
                    else "no IDR frames found in file"
                ),
            })

        if ev := m.start:
            if ev.segmentation_duration_ticks and m.stop:
                declared = ev.segmentation_duration_ticks / PTS_CLOCK
                actual = m.duration_seconds
                drift = abs(actual - declared)
                checks.append({
                    "event_id": m.event_id,
                    "boundary": "start",
                    "check": "duration_matches_declared",
                    "pass": drift <= dur_tol,
                    "detail": f"declared {declared:.3f}s, actual {actual:.3f}s (drift {drift:.3f}s)",
                })
            elif m.splice_type == "time_signal" and not m.is_instant and m.stop is None:
                checks.append({
                    "event_id": m.event_id, "boundary": "start",
                    "check": "has_matching_stop", "pass": False,
                    "detail": "no matching end signal found before EOF",
                })

    return checks


# ── report assembly ────────────────────────────────────────────────────────────

def marker_to_dict(m: Marker) -> dict:
    def ev_dict(e: Optional[RawEvent]) -> Optional[dict]:
        if e is None:
            return None
        d = {
            "pts_ticks": e.pts_ticks,
            "pts_seconds": round(e.pts_seconds, 6),
            "type_id": f"0x{e.segmentation_type_id:02X}" if e.segmentation_type_id is not None else None,
            "segmentation_duration_ticks": e.segmentation_duration_ticks,
            "upid_type": e.upid_type,
            "upid_hex": e.upid_hex,
            "segment_num": e.segment_num,
            "segments_expected": e.segments_expected,
            "flags": e.flags,
        }
        return d

    return {
        "event_id": m.event_id,
        "splice_type": m.splice_type,
        "segmentation_type_id": f"0x{m.segmentation_type_id:02X}" if m.segmentation_type_id is not None else None,
        "type_name": m.type_label,
        "type_code": m.type_code,
        "is_instant": m.is_instant,
        "duration_seconds": round(m.duration_seconds, 6) if m.duration_seconds is not None else None,
        "nesting_depth": m.nesting_depth,
        "contains": m.contains,
        "start": ev_dict(m.start),
        "stop": ev_dict(m.stop),
    }


def build_report(
    ts_path: Path,
    output_dir: Path,
    *,
    pid: Optional[str] = None,
    before: int = 3,
    after: int = 2,
    width: int = 320,
    skip_frames: bool = False,
    idr_tolerance_frames: float = 1.0,
    duration_tolerance_frames: float = 2.0,
    expected_path: Optional[Path] = None,
    progress: Optional[Progress] = None,
) -> dict:
    xml_tmp = output_dir / "_splice-info-tables.xml"
    output_dir.mkdir(parents=True, exist_ok=True)

    run_tsduck_extract(ts_path, xml_tmp, pid=pid)
    events = parse_scte35_xml(xml_tmp)
    xml_tmp.unlink(missing_ok=True)

    info = probe_video_info(ts_path)
    frame_dur = 1.0 / info["fps"] if info["fps"] else 1.0 / 25

    markers = pair_events_into_markers(events)
    infer_nesting(markers)

    idr_times, all_times = load_frame_times(ts_path)
    checks = build_checks(markers, idr_times, frame_dur, idr_tolerance_frames, duration_tolerance_frames)

    frames_by_marker: dict[str, list[dict]] = {}
    if not skip_frames:
        frames_dir = output_dir / "frames"
        frames_dir.mkdir(exist_ok=True)
        boundary_jobs = [
            (m, boundary, ev)
            for m in markers
            for boundary, ev in (("start", m.start), ("stop", m.stop))
            if ev is not None
        ]
        task_id = progress.add_task("[cyan]Extracting boundary frames …", total=len(boundary_jobs)) if progress else None

        for m, boundary, ev in boundary_jobs:
            key = f"evt{m.event_id}_{boundary}"
            shots = []
            for off in list(range(-before, 0)) + list(range(0, after + 1)):
                t = ev.pts_seconds + off * frame_dur
                label = f"{off:+d}" if off != 0 else "+0"
                fname = f"{key}_{label.replace('+', 'p').replace('-', 'm')}.jpg"
                data = extract_frame(ts_path, t, width) if t >= 0 else None
                if data:
                    (frames_dir / fname).write_bytes(data)
                idr = nearest(idr_times, t)
                shots.append({
                    "label": label,
                    "pts_seconds": round(t, 6),
                    "is_idr": bool(idr is not None and abs(idr - t) <= frame_dur * 0.5),
                    "file": f"frames/{fname}" if data else None,
                })
            frames_by_marker[key] = shots
            if progress and task_id is not None:
                progress.advance(task_id)

        if all_times:
            for key, times in (("asset_start", all_times[:ENDCAP_FRAMES]), ("asset_end", all_times[-ENDCAP_FRAMES:])):
                shots = []
                for i, t in enumerate(times):
                    fname = f"{key}_{i}.jpg"
                    data = extract_frame(ts_path, t, width)
                    if data:
                        (frames_dir / fname).write_bytes(data)
                    idr = nearest(idr_times, t)
                    shots.append({
                        "label": str(i),
                        "pts_seconds": round(t, 6),
                        "is_idr": bool(idr is not None and abs(idr - t) <= frame_dur * 0.5),
                        "file": f"frames/{fname}" if data else None,
                    })
                frames_by_marker[key] = shots

    marker_dicts = [marker_to_dict(m) for m in markers]
    expected_info = None
    if expected_path is not None:
        expected_entries = load_expected(expected_path)
        checks = checks + compare_expected(expected_entries, marker_dicts, info["fps"])
        expected_info = {"path": str(expected_path), "name": expected_path.name, "entries": len(expected_entries)}

    report = {
        "tool": "krogh",
        "report_version": REPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "path": str(ts_path),
            "name": ts_path.name,
            **info,
            "scte35_table_id": SCTE35_TABLE_ID,
            "scte35_pid": pid,
        },
        "markers": marker_dicts,
        "expected": expected_info,
        "frames": frames_by_marker,
        "checks": checks,
        "summary": {
            "marker_count": len(markers),
            "checks_total": len(checks),
            "checks_failed": sum(1 for c in checks if not c["pass"]),
        },
    }
    return report


def write_report_json(report: dict, output_dir: Path) -> Path:
    path = output_dir / "scte-report.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    return path


# ── CLI ─────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Scan a .ts file's actual SCTE-35 markers (independent of franken-ts) "
                    "and extract frames around each splice boundary."
    )
    ap.add_argument("ts", nargs="?", help="Input .ts file")
    ap.add_argument("--output", default=None, help="Output directory (default: <ts_stem>_scte/)")
    ap.add_argument("--pid", default=None, help="Restrict tsduck scan to this PID (default: scan all PIDs by table id)")
    ap.add_argument("--before", type=int, default=3, help="Frames to grab before each boundary (default: 3)")
    ap.add_argument("--after", type=int, default=2, help="Frames to grab after each boundary (default: 2)")
    ap.add_argument("--width", type=int, default=320, help="Thumbnail width in pixels (default: 320)")
    ap.add_argument("--skip-frames", action="store_true", help="Metadata-only scan, no ffmpeg frame extraction")
    ap.add_argument("--skip-html", action="store_true", help="Write only scte-report.json, no HTML")
    ap.add_argument("--render-only", metavar="JSON_PATH", default=None,
                    help="Skip scanning entirely; (re)generate HTML from an existing scte-report.json")
    ap.add_argument("--idr-tolerance-frames", type=float, default=1.0)
    ap.add_argument("--duration-tolerance-frames", type=float, default=2.0)
    ap.add_argument("--expected", metavar="MARKERS_JSON", default=None,
                    help="Also compare against this franken-ts markers.json (default: auto-discover "
                         "<stem>.markers.json or markers.json next to the .ts)")
    ap.add_argument("--no-expected", action="store_true",
                    help="Don't compare against any markers.json, even if one is next to the .ts")
    args = ap.parse_args()

    if args.render_only:
        json_path = Path(args.render_only)
        report = json.loads(json_path.read_text())
        out_dir = Path(args.output) if args.output else json_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        html_path = out_dir / "scte-report.html"
        html_path.write_text(render_html(report, base_dir=json_path.parent), encoding="utf-8")
        (out_dir / "scte-filmstrip.html").write_text(render_filmstrip(report, base_dir=json_path.parent), encoding="utf-8")
        console.print(f"[green]Rendered:[/] {html_path.resolve()}")
        return

    if not args.ts:
        ap.error("the following arguments are required: ts (unless --render-only is given)")

    ts_path = Path(args.ts)
    if not ts_path.exists():
        console.print(f"[red]File not found:[/] {ts_path}")
        sys.exit(1)

    out_dir = Path(args.output) if args.output else ts_path.parent / f"{ts_path.stem}_scte"

    console.print()
    console.print(Panel(
        f"[bold]{ts_path.name}[/]\n[dim]→ {out_dir.resolve()}[/]",
        title="[bold blue]krogh[/]", border_style="blue", padding=(0, 2),
    ))
    console.print()

    expected_path: Optional[Path] = None
    if args.expected and args.no_expected:
        ap.error("--expected and --no-expected are mutually exclusive")
    if args.expected:
        expected_path = Path(args.expected)
        if not expected_path.is_file():
            ap.error(f"--expected file not found: {expected_path}")
    elif not args.no_expected:
        expected_path = discover_expected_path(ts_path)

    progress = Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=36), MofNCompleteColumn(), console=console,
    )

    with progress:
        t1 = progress.add_task("[cyan]Scanning SCTE-35 tables (tsduck) …", total=None)
        report = build_report(
            ts_path, out_dir,
            pid=args.pid, before=args.before, after=args.after, width=args.width,
            skip_frames=args.skip_frames,
            idr_tolerance_frames=args.idr_tolerance_frames,
            duration_tolerance_frames=args.duration_tolerance_frames,
            expected_path=expected_path,
            progress=progress,
        )
        progress.update(t1, description=f"[green]✓ {report['summary']['marker_count']} marker(s) found", total=1, completed=1)

        json_path = write_report_json(report, out_dir)

        if not args.skip_html:
            html_path = out_dir / "scte-report.html"
            html_path.write_text(render_html(report, base_dir=out_dir), encoding="utf-8")
            (out_dir / "scte-filmstrip.html").write_text(render_filmstrip(report, base_dir=out_dir), encoding="utf-8")

    console.print()
    failed = report["summary"]["checks_failed"]
    status = f"[bold red]{failed} check(s) failed[/]" if failed else "[bold green]all checks passed[/]"
    console.print(f"  Markers found: [bold]{report['summary']['marker_count']}[/]   {status}")
    exp = report.get("expected")
    console.print(f"  Compared with: [cyan]{exp['name']}[/] ({exp['entries']} expected entries)" if exp
                  else "  Compared with: [dim]nothing (no markers.json found; independent scan only)[/]")
    console.print(f"  [bold green]JSON:[/] [cyan underline]file://{json_path.resolve()}[/]")
    if not args.skip_html:
        console.print(f"  [bold green]HTML:[/] [cyan underline]file://{(out_dir / 'scte-report.html').resolve()}[/]")
        console.print(f"  [bold green]Filmstrip:[/] [cyan underline]file://{(out_dir / 'scte-filmstrip.html').resolve()}[/]")
    console.print()


if __name__ == "__main__":
    main()
