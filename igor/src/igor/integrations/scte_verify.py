"""Wrapper around `krogh` (inspector-krogh's independent SCTE-35
scanner): resolves a playlist's built `.ts`, spawns the scan as a job, and
resolves its JSON/HTML output for the Assemble tab.

Unlike `franken_ts.py`, this tool does not read the playlist at all -- it
scans the assembled `.ts` blind. igor still keys everything off the
playlist name because that's the unit the Assemble tab operates on, and
reuses `franken_ts.output_ts_path`/`_resolve_path` purely to find *which*
file to point the scan at and what to compare its mtime against; the scan
itself gets nothing else from franken-ts.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from igor import paths
from igor.integrations import franken_ts
from igor.jobs.runner import Job, runner


def _output_dir(ts_path: Path) -> Path:
    """Mirrors krogh's own default `--output` naming
    (`<ts_stem>_scte/` next to the `.ts`) -- passed explicitly on the job's
    command line rather than relied on, so this stays correct even if the
    CLI's default ever changes."""
    return ts_path.parent / f"{ts_path.stem}_scte"


def scte_verify_json_path(name: str) -> Path:
    ts_path = franken_ts.output_ts_path(name)
    return _output_dir(ts_path) / "scte-report.json"


def scte_verify_html_path(name: str, view: str = "filmstrip") -> Path:
    ts_path = franken_ts.output_ts_path(name)
    filename = "scte-report.html" if view == "cards" else "scte-filmstrip.html"
    return _output_dir(ts_path) / filename


def scte_verify_status(name: str) -> dict:
    """Exists/stale vs the *built `.ts`*'s mtime (not the playlist YAML --
    this report verifies the assembled file itself, so only a reassemble
    that changes the `.ts` should invalidate it)."""
    ts_path = franken_ts.output_ts_path(name)
    json_path = scte_verify_json_path(name)
    exists = json_path.is_file()
    stale = (not exists) or (not ts_path.is_file()) or (
        json_path.stat().st_mtime < ts_path.stat().st_mtime
    )
    return {"exists": exists, "stale": stale}


def scte_verify_report(name: str) -> dict:
    json_path = scte_verify_json_path(name)
    if not json_path.is_file():
        raise FileNotFoundError(f"No SCTE-35 verify report for playlist {name!r} yet")
    return json.loads(json_path.read_text(encoding="utf-8"))


def spawn_scte_verify_job(name: str) -> Job:
    ts_path = franken_ts.output_ts_path(name)
    if not ts_path.is_file():
        raise FileNotFoundError(f"Playlist {name!r} has not been assembled yet -- no {ts_path} to scan")
    out_dir = _output_dir(ts_path)
    cmd = paths.scte_verify_python() + [str(ts_path), "--output", str(out_dir)]
    return runner.spawn("scte-verify", cmd, cwd=paths.REPO_ROOT, channel_name=name)


def _template_mtime() -> float:
    """Newest mtime among inspector-krogh's Python sources (the HTML
    templates live there), so template edits invalidate rendered HTML."""
    sources = (paths.REPO_ROOT / "inspector-krogh").glob("*.py")
    return max((p.stat().st_mtime for p in sources), default=0.0)


def ensure_html(name: str, view: str = "filmstrip") -> Path:
    """Path to the rendered HTML, regenerated from the saved JSON first if
    it is missing or older than the JSON or the report templates. Rendering
    is a pure JSON -> HTML step (`krogh --render-only`), so this never
    re-scans the `.ts`. If re-rendering fails, an existing file is served
    as-is rather than failing the request."""
    html_path = scte_verify_html_path(name, view)
    json_path = scte_verify_json_path(name)
    if not json_path.is_file():
        return html_path
    newest_input = max(json_path.stat().st_mtime, _template_mtime())
    if html_path.is_file() and html_path.stat().st_mtime >= newest_input:
        return html_path
    proc = subprocess.run(
        paths.scte_verify_python() + ["--render-only", str(json_path)],
        cwd=paths.REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 and not html_path.is_file():
        raise RuntimeError(f"krogh --render-only failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return html_path
