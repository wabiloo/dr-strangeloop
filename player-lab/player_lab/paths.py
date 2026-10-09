"""Locations: repo root, harness assets, the SDK cache and the output folder."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parents[1]
HARNESS_DIR = PACKAGE_DIR / "harness"
VENDOR_PACKAGE_JSON = PACKAGE_DIR / "vendor" / "package.json"
ITS_A_LIVE_DIR = REPO_ROOT / "its-a-live"

# dr_strangeloop_config.py lives at the repo root (stdlib only); this package
# is not installed alongside it.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from dr_strangeloop_config import get_paths  # noqa: E402


def cache_dir() -> Path:
    """SDK cache (node_modules with dash.js, Shaka, ...). Never in the repo."""
    env = os.environ.get("PLAYER_LAB_HOME")
    return Path(os.path.expanduser(env)) if env else Path.home() / ".dr-strangeloop" / "player-lab"


def vendor_root() -> Path:
    return cache_dir() / "node_modules"


def outputs_dir() -> Path:
    return get_paths().outputs_dir / "player-lab"


def channels_dir() -> Path:
    return get_paths().channels_dir
