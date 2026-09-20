"""Repo layout constants shared across loop-console's backend."""

from __future__ import annotations

import shutil
from pathlib import Path

# loop-console/src/loop_console/paths.py -> repo root is 4 parents up.
LOOP_CONSOLE_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = LOOP_CONSOLE_DIR.parent

FRANKEN_TS_DIR = REPO_ROOT / "franken-ts"
ITS_A_LIVE_DIR = REPO_ROOT / "its-a-live"
LOOP_DEE_LOOP_DIR = REPO_ROOT / "loop-dee-loop"

FRANKEN_TS_CONFIGS_DIR = FRANKEN_TS_DIR / "configs"
ITS_A_LIVE_CONFIGS_DIR = REPO_ROOT / "configs"
OUTPUTS_DIR = REPO_ROOT / "outputs"


def its_a_live_python() -> list[str]:
    """Resolution order mirrors loop-dee-loop's ops modules: its-a-live
    manages its own separate venv (aws-cdk-lib/boto3 kept isolated from
    media tooling), so we must call `channel.py` with *that* Python, not
    whatever's running loop-console itself."""
    venv_python = ITS_A_LIVE_DIR / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return [str(venv_python)]
    if shutil.which("uv"):
        return ["uv", "run", "--project", str(ITS_A_LIVE_DIR), "python"]
    return ["python3"]


def franken_ts_python() -> list[str]:
    """franken-ts is a workspace member sharing loop-console's own venv,
    so in practice `sys.executable` already works -- but subprocess calls
    still go through `uv run` for parity/robustness when invoked from a
    different environment (e.g. a systemd unit with a bare `python3`)."""
    if shutil.which("uv"):
        return ["uv", "run", "--project", str(REPO_ROOT), "franken-ts"]
    return ["franken-ts"]
