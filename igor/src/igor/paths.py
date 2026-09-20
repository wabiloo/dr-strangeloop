"""Repo layout constants shared across igor's backend."""

from __future__ import annotations

import shutil
from pathlib import Path

# igor/src/igor/paths.py -> repo root is 4 parents up.
IGOR_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = IGOR_DIR.parent

FRANKEN_TS_DIR = REPO_ROOT / "franken-ts"
ITS_A_LIVE_DIR = REPO_ROOT / "its-a-live"
LOOP_DEE_LOOP_DIR = REPO_ROOT / "loop-dee-loop"

FRANKEN_TS_PLAYLISTS_DIR = FRANKEN_TS_DIR / "playlists"
ITS_A_LIVE_CONFIGS_DIR = REPO_ROOT / "configs"
OUTPUTS_DIR = REPO_ROOT / "outputs"


def its_a_live_python() -> list[str]:
    """Resolution order mirrors loop-dee-loop's ops modules: its-a-live
    manages its own separate venv (aws-cdk-lib/boto3 kept isolated from
    media tooling), so we must call `channel.py` with *that* Python, not
    whatever's running igor itself."""
    venv_python = ITS_A_LIVE_DIR / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return [str(venv_python)]
    if shutil.which("uv"):
        return ["uv", "run", "--project", str(ITS_A_LIVE_DIR), "python"]
    return ["python3"]


def franken_ts_python() -> list[str]:
    """franken-ts is a workspace member sharing igor's own venv,
    so in practice `sys.executable` already works -- but subprocess calls
    still go through `uv run` for parity/robustness when invoked from a
    different environment (e.g. a systemd unit with a bare `python3`)."""
    if shutil.which("uv"):
        return ["uv", "run", "--project", str(REPO_ROOT), "franken-ts"]
    return ["franken-ts"]
