"""Thin wrapper around franken-ts: config discovery/read/write, JSON Schema
for the frontend's visual editor, and build-job dispatch.

franken-ts is a workspace member sharing igor's own venv (see
paths.py), so its Pydantic models are imported directly here -- the
editor's schema is generated from `franken_ts.config.Config` itself
(`model_json_schema()`), not hand-duplicated, so it can never drift from
what franken-ts actually accepts.
"""

from __future__ import annotations

import yaml
from franken_ts.config import Config

from igor import paths
from igor.jobs.runner import Job, runner


def config_schema() -> dict:
    """JSON Schema for franken-ts's top-level YAML config, straight from
    the Pydantic model franken-ts itself validates against."""
    return Config.model_json_schema()


def list_configs() -> list[dict]:
    """All *.yaml configs in franken-ts/configs/, with just enough parsed
    metadata for a list view (name, output path, asset/ad-break counts) --
    full content is fetched separately via get_config."""
    out = []
    for path in sorted(paths.FRANKEN_TS_CONFIGS_DIR.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            out.append({"name": path.stem, "path": str(path), "error": str(exc)})
            continue
        output = raw.get("output", {}) or {}
        assets = raw.get("assets", []) or []
        out.append({
            "name": path.stem,
            "path": str(path),
            "output_file": output.get("file"),
            "output_dir": output.get("dir"),
            "asset_count": len(assets),
            "ad_break_count": sum(1 for a in assets if isinstance(a, dict) and a.get("ad_break")),
        })
    return out


def get_config(name: str) -> dict:
    path = _resolve_path(name)
    return yaml.safe_load(path.read_text()) or {}


def save_config(name: str, data: dict) -> str:
    """Validates against franken_ts.config.Config before writing, so a bad
    form submission never reaches disk as an unusable YAML file."""
    Config.model_validate(data)
    path = _resolve_path(name, must_exist=False)
    paths.FRANKEN_TS_CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return str(path)


def delete_config(name: str) -> None:
    _resolve_path(name).unlink()


def _resolve_path(name: str, must_exist: bool = True):
    if "/" in name or "\\" in name or name in ("..", "."):
        raise ValueError(f"Invalid config name: {name!r}")
    path = paths.FRANKEN_TS_CONFIGS_DIR / f"{name}.yaml"
    if must_exist and not path.is_file():
        raise FileNotFoundError(f"No such franken-ts config: {name}")
    return path


def spawn_build_job(name: str, extra_args: list[str] | None = None) -> Job:
    config_path = _resolve_path(name)
    cmd = paths.franken_ts_python() + [str(config_path), *(extra_args or [])]
    return runner.spawn("build", cmd, cwd=paths.REPO_ROOT, channel_name=name)
