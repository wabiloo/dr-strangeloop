"""Thin wrapper around its-a-live/channel.py.

its-a-live manages its own separate venv (aws-cdk-lib/boto3 kept isolated
from media tooling -- see repo AGENTS.md), so every call here shells out
to `channel.py` using that venv's Python (paths.its_a_live_python()) --
this module intentionally does not import boto3/its-a-live code directly.

Two kinds of operations:
- Fast, read-only ones (`list_channels`, `get_status`, `get_outputs`) are
  run synchronously (single AWS API call, <1s) and return parsed JSON.
- Long-running ones (`create`, `spark`, `start`, `stop`, `refresh`,
  `redeploy`) are dispatched as background Jobs (see jobs/runner.py) since
  they can block for minutes (cdk deploy, ECS Express canary, etc).
"""

from __future__ import annotations

import json
import subprocess

from igor import paths
from igor.jobs.runner import Job, runner

_TOML_TEMPLATE = """\
[deploy]
name = "{name}"
backend = "{backend}"

[aws]
region = "{region}"

[s3]
bucket_name = "{bucket_name}"
content_folder = "{content_folder}"

[input]
source_path = "{source_path}"
"""

_ECS_EXPRESS_EXTRA = """
[channel]
segment_duration   = {segment_duration}
dvr_window_seconds = {dvr_window_seconds}
port               = {port}

[express]
cpu    = {cpu}
memory = {memory}
"""


def generate_toml(
    *,
    name: str,
    backend: str,
    region: str,
    bucket_name: str,
    content_folder: str,
    source_path: str,
    segment_duration: float = 4.0,
    dvr_window_seconds: float = 30,
    port: int = 8080,
    cpu: int = 256,
    memory: int = 512,
) -> str:
    if backend not in ("aws-media", "ecs-express"):
        raise ValueError(f"backend must be 'aws-media' or 'ecs-express', got {backend!r}")
    content = _TOML_TEMPLATE.format(
        name=name, backend=backend, region=region, bucket_name=bucket_name,
        content_folder=content_folder, source_path=source_path,
    )
    if backend == "ecs-express":
        content += _ECS_EXPRESS_EXTRA.format(
            segment_duration=segment_duration, dvr_window_seconds=dvr_window_seconds,
            port=port, cpu=cpu, memory=memory,
        )
    return content


def _channel_py_cmd(extra: list[str], config_path: str | None = None, as_json: bool = True) -> list[str]:
    cmd = list(paths.its_a_live_python()) + ["channel.py"]
    if config_path is not None:
        cmd += ["--config", config_path]
    if as_json:
        cmd += ["--json"]
    return cmd + extra


def _run_json(extra: list[str], config_path: str | None = None) -> dict | list:
    cmd = _channel_py_cmd(extra, config_path=config_path, as_json=True)
    result = subprocess.run(
        cmd, cwd=paths.ITS_A_LIVE_DIR, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"`{' '.join(cmd)}` failed (exit {result.returncode}):\n{result.stderr or result.stdout}"
        )
    # channel.py prints the human-readable line THEN the json line for
    # status/outputs (see channel.py's cmd_status/cmd_outputs) -- json is
    # always the last non-empty line.
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"`{' '.join(cmd)}` produced no output")
    return json.loads(lines[-1])


def list_channels(configs_dir: str | None = None) -> list[dict]:
    extra = ["list"]
    if configs_dir:
        extra.append(configs_dir)
    return _run_json(extra)


def get_status(config_path: str) -> dict:
    return _run_json(["status"], config_path=config_path)


def get_outputs(config_path: str) -> dict:
    return _run_json(["outputs"], config_path=config_path)


def spawn_job(job_type: str, config_path: str, channel_name: str, extra_args: list[str] | None = None) -> Job:
    """Dispatches one of the long-running channel.py subcommands
    (create/spark/start/stop/refresh/redeploy) as a background Job."""
    if job_type not in ("create", "spark", "start", "stop", "refresh", "redeploy"):
        raise ValueError(f"Unsupported job_type: {job_type!r}")
    cmd = _channel_py_cmd([job_type, *(extra_args or [])], config_path=config_path, as_json=False)
    return runner.spawn(job_type, cmd, cwd=paths.ITS_A_LIVE_DIR, channel_name=channel_name)
