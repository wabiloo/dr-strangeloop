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
GRAVE_ROBBER_DIR = REPO_ROOT / "grave-robber"

FRANKEN_TS_PLAYLISTS_DIR = REPO_ROOT / "data" / "playlists"
ITS_A_LIVE_CONFIGS_DIR = REPO_ROOT / "data" / "channels"
ASSET_UPLOADS_DIR = REPO_ROOT / "data" / "assets"
OUTPUTS_DIR = REPO_ROOT / "outputs"

# grave-robber/SCOPE.md §10: "available archives (HAR/Proxyman logs dropped
# into a store this tool owns, e.g. data/archives/)".
ARCHIVES_DIR = REPO_ROOT / "data" / "archives"
# Import output lands alongside franken-ts's own outputs/ tree, in its own
# subdirectory so a grave-robber import (manifest.json + media/) never
# collides with a franken-ts playlist's own <name>.ts/<name>/ output.
ARCHIVE_IMPORTS_DIR = OUTPUTS_DIR / "archives"

# VOD manifest-URL sources (`grave-robber ingest-url`): the saved URL +
# display name live under data/manifests/, the downloaded import
# (manifest.json + media/) under outputs/manifests/<name>/.
MANIFESTS_DIR = REPO_ROOT / "data" / "manifests"
MANIFEST_IMPORTS_DIR = OUTPUTS_DIR / "manifests"


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
