"""Licence keys for commercial players: environment first, then [keys] in the dr-strangeloop config.toml."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from . import paths  # noqa: F401  (puts the repo root on sys.path for dr_strangeloop_config)
from dr_strangeloop_config import find_config_file

ENV_VARS = {"bitmovin": "BITMOVIN_LICENSE_KEY"}
KEYED_PLAYERS = tuple(ENV_VARS)


def key_file() -> Path:
    """The file the keys are read from: ~/.dr-strangeloop/config.toml (or $DR_STRANGELOOP_CONFIG)."""
    return find_config_file() or Path.home() / ".dr-strangeloop" / "config.toml"


def load_keys() -> dict[str, str]:
    """player id -> licence key, for the players that have one."""
    keys: dict[str, str] = {}
    f = key_file()
    if f.is_file():
        with f.open("rb") as fh:
            section = tomllib.load(fh).get("keys", {})
        keys.update({p: str(v) for p, v in section.items() if p in ENV_VARS and v})
    for player, var in ENV_VARS.items():
        if os.environ.get(var):
            keys[player] = os.environ[var]
    return keys


def available() -> dict[str, bool]:
    k = load_keys()
    return {p: p in k for p in KEYED_PLAYERS}
