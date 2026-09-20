"""Channel definitions = its-a-live TOML configs (data/channels/*.toml at
the repo root, the same convention `channel.py`/`galvanise.py` already use --
igor does not invent a separate storage format so channels stay
fully manageable from the CLI too, not console-only)."""

from __future__ import annotations

import tomllib

from igor import paths


def list_channel_paths() -> list[str]:
    paths.ITS_A_LIVE_CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    return [str(p) for p in sorted(paths.ITS_A_LIVE_CONFIGS_DIR.glob("*.toml"))]


def config_path_for(name: str) -> str:
    if "/" in name or "\\" in name or name in ("..", "."):
        raise ValueError(f"Invalid channel name: {name!r}")
    return str(paths.ITS_A_LIVE_CONFIGS_DIR / f"{name}.toml")


def read_channel_config(name: str) -> dict:
    path = config_path_for(name)
    with open(path, "rb") as f:
        return tomllib.load(f)


def write_channel_config(name: str, toml_content: str) -> str:
    path = config_path_for(name)
    paths.ITS_A_LIVE_CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(toml_content)
    return path


def delete_channel_config(name: str) -> None:
    from pathlib import Path

    Path(config_path_for(name)).unlink()
