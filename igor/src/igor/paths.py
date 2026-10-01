"""Repo layout constants shared across igor's backend."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

# igor/src/igor/paths.py -> repo root is 4 parents up.
IGOR_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = IGOR_DIR.parent

# dr_strangeloop_config.py lives at the repo root (stdlib-only, shared with
# galvanise.py and its-a-live) and igor is not installed with it on sys.path.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from dr_strangeloop_config import get_paths  # noqa: E402

FRANKEN_TS_DIR = REPO_ROOT / "franken-ts"
ITS_A_LIVE_DIR = REPO_ROOT / "its-a-live"
LOOP_DEE_LOOP_DIR = REPO_ROOT / "loop-dee-loop"
GRAVE_ROBBER_DIR = REPO_ROOT / "grave-robber"

_cfg = get_paths()

FRANKEN_TS_PLAYLISTS_DIR = _cfg.playlists_dir
ITS_A_LIVE_CONFIGS_DIR = _cfg.channels_dir
ASSET_UPLOADS_DIR = _cfg.assets_dir
OUTPUTS_DIR = _cfg.outputs_dir

# grave-robber archive captures (HAR/Proxyman logs) and their imports.
ARCHIVES_DIR = _cfg.archives_dir
ARCHIVE_IMPORTS_DIR = _cfg.archive_imports_dir

# VOD manifest-URL sources (`grave-robber ingest-url`): saved URL + display
# name, and the downloaded import (manifest.json + media/).
MANIFESTS_DIR = _cfg.manifests_dir
MANIFEST_IMPORTS_DIR = _cfg.manifest_imports_dir


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


def grave_robber_python() -> list[str]:
    """grave-robber is a workspace member sharing igor's own venv (like
    franken-ts) -- `uv run` for parity/robustness across environments,
    same reasoning as franken_ts_python()."""
    if shutil.which("uv"):
        return ["uv", "run", "--project", str(REPO_ROOT), "grave-robber"]
    return ["grave-robber"]


def scte_verify_python() -> list[str]:
    """inspector-krogh (which owns `krogh`) is a repo-root workspace
    member but, unlike franken-ts, is *not* one of igor's own declared
    dependencies -- deliberately, since the whole point of krogh is
    that it scans a built `.ts` with no franken-ts/playlist involvement, so
    igor shells out to it via `uv run` exactly like it does for the other
    workspace-adjacent tools, rather than importing it."""
    if shutil.which("uv"):
        return ["uv", "run", "--project", str(REPO_ROOT), "krogh"]
    return ["krogh"]
