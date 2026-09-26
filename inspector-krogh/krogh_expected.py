"""Optional comparison of what krogh found in a .ts against what the build
*meant* to put there (a franken-ts `.markers.json`).

Pure functions over plain dicts -- no ffmpeg/tsduck -- so they're unit
testable and reusable. The scan itself never looks at the expected file;
this only adds extra checks on top of an already independent scan.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

PTS_CLOCK = 90_000
_BOOL_FLAGS = ("web_delivery_allowed", "no_regional_blackout", "archive_allowed")


def discover_expected_path(ts_path: Path) -> Optional[Path]:
    """`<stem>.markers.json` next to the .ts, else `markers.json` in its
    directory (the rendition-ladder layout) -- same convention franken-ts
    writes and loop-dee-loop reads."""
    for candidate in (ts_path.with_suffix(".markers.json"), ts_path.parent / "markers.json"):
        if candidate.is_file():
            return candidate
    return None


def load_expected(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list of marker entries")
    return data


def _int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    return value if isinstance(value, int) else int(str(value), 0)


def _hex(value) -> str:
    return "".join(str(value or "").split()).lower()


def _key(event_id, is_start: bool) -> tuple[int, str]:
    return _int(event_id), "start" if is_start else "stop"


def _fmt_delta(ticks: int) -> str:
    return f"{ticks / PTS_CLOCK:+.3f}s"


def _mismatches(exp: dict, act: dict, marker: dict, boundary: str, tol_ticks: float) -> tuple[list[str], int]:
    """(field mismatches, pts delta in ticks) between one expected entry and
    the actual event it was paired with."""
    out: list[str] = []
    delta = act["pts_ticks"] - _int(exp["pts_time_ticks"])
    if abs(delta) > tol_ticks:
        out.append(f"pts {_fmt_delta(delta)} off")

    exp_type = _int(exp.get("segmentation_type_id"))
    act_type = _int(act.get("type_id"))
    if exp_type is not None and act_type is not None and exp_type != act_type:
        out.append(f"type_id expected 0x{exp_type:02X}, found 0x{act_type:02X}")

    if boundary == "start":
        exp_dur = _int(exp.get("segmentation_duration_ticks")) or 0
        act_dur = act.get("segmentation_duration_ticks") or 0
        if exp_dur != act_dur:
            out.append(f"duration expected {exp_dur / PTS_CLOCK:g}s, found {act_dur / PTS_CLOCK:g}s")

    for name in ("segment_num", "segments_expected"):
        e, a = _int(exp.get(name)), act.get(name)
        if e is not None and a is not None and e != a:
            out.append(f"{name} expected {e}, found {a}")

    if exp.get("upid_hex") and act.get("upid_hex") and _hex(exp["upid_hex"]) != _hex(act["upid_hex"]):
        out.append(f"upid expected {_hex(exp['upid_hex'])}, found {_hex(act['upid_hex'])}")
    e_ut, a_ut = _int(exp.get("upid_type")), _int(act.get("upid_type"))
    if e_ut is not None and a_ut is not None and e_ut != a_ut:
        out.append(f"upid_type expected 0x{e_ut:02X}, found 0x{a_ut:02X}")

    e_flags, a_flags = exp.get("flags") or {}, act.get("flags") or {}
    for name in _BOOL_FLAGS:
        if name in e_flags and name in a_flags and bool(e_flags[name]) != bool(a_flags[name]):
            out.append(f"{name} expected {bool(e_flags[name])}, found {bool(a_flags[name])}")
    if "device_restrictions" in e_flags and "device_restrictions" in a_flags:
        if int(e_flags["device_restrictions"]) != int(a_flags["device_restrictions"]):
            out.append(
                f"device_restrictions expected {int(e_flags['device_restrictions'])}, "
                f"found {int(a_flags['device_restrictions'])}"
            )
    return out, delta


def compare_expected(expected: list[dict], markers: list[dict], fps: float) -> list[dict]:
    """Return check rows (same shape as krogh's other checks) comparing the
    expected entries with the scanned `markers` (marker_to_dict output).

    Also sets `marker["assets"]` on scanned markers whose expected entry
    lists the assets that make up the span."""
    tol_ticks = PTS_CLOCK / (fps or 25.0)

    actual: dict[tuple[int, str], list[tuple[dict, dict]]] = {}
    for m in markers:
        for boundary in ("start", "stop"):
            ev = m.get(boundary)
            if ev is not None:
                actual.setdefault((m["event_id"], boundary), []).append((m, ev))
    exp_by_key: dict[tuple[int, str], list[dict]] = {}
    for e in expected:
        exp_by_key.setdefault(_key(e["event_id"], bool(e.get("is_out"))), []).append(e)

    checks: list[dict] = []
    for key in sorted(set(actual) | set(exp_by_key)):
        eid, boundary = key
        exps = sorted(exp_by_key.get(key, []), key=lambda e: _int(e["pts_time_ticks"]))
        acts = sorted(actual.get(key, []), key=lambda p: p[1]["pts_ticks"])
        for i in range(max(len(exps), len(acts))):
            base = {"event_id": eid, "boundary": boundary}
            if i >= len(acts):
                checks.append({**base, "check": "expected_present", "pass": False,
                               "detail": f"in markers.json at {_int(exps[i]['pts_time_ticks']) / PTS_CLOCK:.3f}s but not found in the stream"})
            elif i >= len(exps):
                checks.append({**base, "check": "unexpected_marker", "pass": False,
                               "detail": f"found in the stream at {acts[i][1]['pts_ticks'] / PTS_CLOCK:.3f}s but not in markers.json"})
            else:
                marker, ev = acts[i]
                bad, delta = _mismatches(exps[i], ev, marker, boundary, tol_ticks)
                if exps[i].get("assets"):
                    marker["assets"] = exps[i]["assets"]
                checks.append({**base, "check": "matches_expected", "pass": not bad,
                               "detail": "; ".join(bad) if bad else f"pts {_fmt_delta(delta)} · all fields match markers.json"})
    return checks


# ── asset timeline (franken-ts `.timeline.json`) ─────────────────────────────

def discover_timeline_path(ts_path: Path) -> Optional[Path]:
    for candidate in (ts_path.with_suffix(".timeline.json"), ts_path.parent / "timeline.json"):
        if candidate.is_file():
            return candidate
    return None


def load_timeline(path: Path) -> dict:
    doc = json.loads(path.read_text())
    if not isinstance(doc, dict) or "entries" not in doc:
        raise ValueError(f"{path}: expected a franken-ts timeline document")
    return doc


def timeline_assets(doc: dict) -> list[dict]:
    """Assets as (stream-time) spans."""
    off = float(doc.get("muxer_offset") or 0.0)
    return [
        {"asset_id": e.get("asset_id"), "file": e.get("source_file"),
         "start": round(e["output_start"] + off, 6), "end": round(e["output_end"] + off, 6)}
        for e in doc["entries"]
    ]


def find_transitions(assets: list[dict], markers: list[dict], fps: float) -> list[dict]:
    """Joins between consecutive assets that carry no SCTE-35 marker
    boundary (within one frame)."""
    tol = 1.0 / (fps or 25.0)
    boundary_times = [
        ev["pts_seconds"] for m in markers for ev in (m.get("start"), m.get("stop")) if ev is not None
    ]
    out = []
    for i in range(1, len(assets)):
        t = assets[i]["start"]
        if any(abs(t - b) <= tol for b in boundary_times):
            continue
        out.append({
            "index": len(out) + 1, "time": t,
            "from_asset": assets[i - 1]["asset_id"], "from_file": assets[i - 1]["file"],
            "to_asset": assets[i]["asset_id"], "to_file": assets[i]["file"],
        })
    return out
