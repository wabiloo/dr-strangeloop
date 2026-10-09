"""Wrapper around `player-lab` (multi-player playback validator).

Like `scte_verify`, player-lab is a workspace member that igor does not
depend on: it is run through `uv run` as a job. It needs the installed
Chrome and the player SDKs (`player-lab setup`) on the machine igor runs on,
which is why `info()` is exposed to the UI.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
from pathlib import Path

from igor import paths
from igor.jobs.runner import Job, runner

_RUN_ID = re.compile(r"^\d{8}-\d{6}$")


def player_lab_cmd() -> list[str]:
    if shutil.which("uv"):
        return ["uv", "run", "--project", str(paths.REPO_ROOT), "--package", "player-lab", "player-lab"]
    return ["player-lab"]


def runs_root(channel: str) -> Path:
    return paths.OUTPUTS_DIR / "player-lab" / "channels" / channel


def info() -> dict:
    proc = subprocess.run([*player_lab_cmd(), "info"], capture_output=True, text=True, timeout=60, cwd=paths.REPO_ROOT)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "player-lab info failed")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def spawn_setup() -> Job:
    return runner.spawn("playback-setup", [*player_lab_cmd(), "setup"], cwd=paths.REPO_ROOT)


def spawn_run(
    channel: str,
    config_path: str,
    *,
    players: list[str] | None,
    formats: list[str] | None,
    boundaries: int,
    duration_s: int,
    max_seconds: int,
    ffmpeg_s: int | None,
) -> tuple[Job, str]:
    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = runs_root(channel) / run_id
    cmd = [*player_lab_cmd(), "run", "--channel", config_path, "--out-dir", str(out_dir),
           "--boundaries", str(boundaries), "--duration", str(duration_s), "--max-seconds", str(max_seconds)]
    if players:
        cmd += ["--players", ",".join(players)]
    if formats:
        cmd += ["--format", ",".join(formats)]
    if ffmpeg_s:
        cmd += ["--ffmpeg", str(ffmpeg_s)]
    return runner.spawn("playback-test", cmd, cwd=paths.REPO_ROOT, channel_name=channel), run_id


def run_id_of(job: Job) -> str | None:
    """The run folder a playback-test job writes to (read back off its command line)."""
    if "--out-dir" not in job.command:
        return None
    return Path(job.command[job.command.index("--out-dir") + 1]).name


def read_report(channel: str, run_id: str) -> dict | None:
    if not _RUN_ID.match(run_id):
        raise ValueError(f"bad run id {run_id!r}")
    path = runs_root(channel) / run_id / "report.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def screenshot_path(channel: str, run_id: str, filename: str) -> Path | None:
    if not _RUN_ID.match(run_id) or not re.fullmatch(r"[A-Za-z0-9_-]+\.png", filename):
        return None
    path = runs_root(channel) / run_id / filename
    return path if path.is_file() else None


def summarise(run_id: str, report: dict) -> dict:
    cases = report.get("cases", [])
    return {
        "run_id": run_id,
        "generated_at": report.get("generatedAt"),
        "duration_s": report.get("durationS"),
        "passed": bool(report.get("pass")),
        "cases": len(cases),
        "failed_cases": sum(1 for c in cases if not c.get("pass")),
        "players": sorted({c["player"] for c in cases}),
    }


def list_runs(channel: str, limit: int = 20) -> list[dict]:
    root = runs_root(channel)
    if not root.is_dir():
        return []
    out = []
    for d in sorted((d for d in root.iterdir() if d.is_dir() and _RUN_ID.match(d.name)), reverse=True)[:limit]:
        try:
            report = json.loads((d / "report.json").read_text())
        except (OSError, ValueError):
            continue  # still running, or a crashed run without a report
        out.append(summarise(d.name, report))
    return out
