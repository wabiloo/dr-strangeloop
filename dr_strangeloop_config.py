"""Single source of truth for where dr-strangeloop keeps its data and outputs.

Standard library only: igor, galvanise.py and its-a-live (which has its own
venv) all import this one file.

Config file lookup (first hit wins):
  1. $DR_STRANGELOOP_CONFIG (a config file, or a directory containing one)
  2. ~/.dr-strangeloop/config.toml
  3. none found -> built-in defaults (the repo-relative layout)

Relative paths in the file resolve against the directory holding it, and
keys left out default to folders inside it (`data/`, `outputs/`,
`packages/`), so ~/.dr-strangeloop/ is a self-contained workspace and
nothing lands in the home directory itself. Only with no config file at
all do the repo-relative defaults apply.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

CONFIG_DIRNAME = ".dr-strangeloop"
CONFIG_FILENAME = "config.toml"
ENV_VAR = "DR_STRANGELOOP_CONFIG"

REPO_ROOT = Path(__file__).resolve().parent

_PATH_KEYS = (
    "data_dir", "outputs_dir", "playlists_dir", "channels_dir", "assets_dir",
    "archives_dir", "manifests_dir", "local_package_dir",
)


@dataclass(frozen=True)
class Paths:
    config_file: Path | None
    data_dir: Path
    outputs_dir: Path
    playlists_dir: Path
    channels_dir: Path
    assets_dir: Path
    archives_dir: Path
    manifests_dir: Path
    archive_imports_dir: Path
    manifest_imports_dir: Path
    local_package_dir: Path


def find_config_file() -> Path | None:
    env = os.environ.get(ENV_VAR)
    if env:
        p = Path(os.path.expanduser(env))
        if p.is_dir():
            p = p / CONFIG_FILENAME
        if not p.is_file():
            raise FileNotFoundError(f"{ENV_VAR}={env!r} does not point to a config file")
        return Path(os.path.abspath(p))

    home_config = Path.home() / CONFIG_DIRNAME / CONFIG_FILENAME
    return Path(os.path.abspath(home_config)) if home_config.is_file() else None


def load_paths(config_file: Path | None) -> Paths:
    raw: dict = {}
    base = REPO_ROOT
    if config_file is not None:
        with open(config_file, "rb") as f:
            raw = tomllib.load(f).get("paths", {})
        base = config_file.parent
        unknown = sorted(set(raw) - set(_PATH_KEYS))
        if unknown:
            raise ValueError(
                f"{config_file}: unknown [paths] key(s) {unknown}; valid keys: {list(_PATH_KEYS)}"
            )

    def pick(key: str, default: Path) -> Path:
        value = raw.get(key)
        if value is None:
            return default
        p = Path(os.path.expanduser(str(value)))
        return Path(os.path.normpath(p if p.is_absolute() else base / p))

    if config_file is None:
        default_data, default_outputs = REPO_ROOT / "data", REPO_ROOT / "outputs"
        default_packages = REPO_ROOT / "its-a-live" / ".local-loop-package"
    else:
        default_data, default_outputs, default_packages = base / "data", base / "outputs", base / "packages"

    data_dir = pick("data_dir", default_data)
    outputs_dir = pick("outputs_dir", default_outputs)
    return Paths(
        config_file=config_file,
        data_dir=data_dir,
        outputs_dir=outputs_dir,
        playlists_dir=pick("playlists_dir", data_dir / "playlists"),
        channels_dir=pick("channels_dir", data_dir / "channels"),
        assets_dir=pick("assets_dir", data_dir / "assets"),
        archives_dir=pick("archives_dir", data_dir / "archives"),
        manifests_dir=pick("manifests_dir", data_dir / "manifests"),
        archive_imports_dir=outputs_dir / "archives",
        manifest_imports_dir=outputs_dir / "manifests",
        local_package_dir=pick("local_package_dir", default_packages),
    )


@lru_cache(maxsize=1)
def get_paths() -> Paths:
    return load_paths(find_config_file())


def resolve_source_path(source_path: str, config_path: str | os.PathLike | None = None) -> str:
    """Absolute form of a channel TOML's [input].source_path.

    Relative paths resolve against the channel TOML's directory. Configs
    written before that rule used `../outputs/x.ts`, relative to the
    working directory (its-a-live/), so that form is still honoured when
    nothing exists at the TOML-relative location.
    """
    if not source_path:
        return source_path
    p = os.path.expanduser(source_path)
    if os.path.isabs(p):
        return os.path.normpath(p)
    from_cwd = os.path.abspath(p)
    if config_path is None:
        return from_cwd
    from_toml = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(config_path)), p))
    if os.path.exists(from_toml) or not os.path.exists(from_cwd):
        return from_toml
    return from_cwd
