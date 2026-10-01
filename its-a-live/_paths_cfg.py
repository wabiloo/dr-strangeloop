"""Loads the repo-root dr_strangeloop_config.py (stdlib-only; its-a-live has
its own venv, so it can't be a workspace dependency)."""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dr_strangeloop_config import get_paths, resolve_source_path  # noqa: E402,F401
