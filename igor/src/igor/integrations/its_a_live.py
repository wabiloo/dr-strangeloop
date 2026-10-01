"""Thin wrapper around its-a-live/channel.py.

its-a-live manages its own separate venv (aws-cdk-lib/boto3 kept isolated
from media tooling -- see repo AGENTS.md), so every call here shells out
to `channel.py` using that venv's Python (paths.its_a_live_python()) --
this module intentionally does not import boto3/its-a-live code directly.

Two kinds of operations:
- Fast, read-only ones (`list_channels`, `get_status`, `get_outputs`) are
  run synchronously (single AWS API call, <1s) and return parsed JSON.
- Long-running ones (`create`, `spark`, `start`, `stop`, `refresh`,
  `update`, `redeploy`, `terminate`) are dispatched as background Jobs
  (see jobs/runner.py) since they can block for minutes (cdk
  deploy/destroy, ECS Express canary, etc).
"""

from __future__ import annotations

import datetime
import json
import subprocess
import string

from igor import paths
from igor.jobs.runner import Job, runner

DEFAULT_DATERANGE_ID_FORMAT = "{segcode}-{eventid}-{loop}"
# Mirrors its-a-live/_epoch_cfg.py (that venv is separate, so not imported).
DEFAULT_EPOCH_UTC = "2026-01-01T00:00:00Z"
_EPOCH_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_DATERANGE_ID_FIELDS = {"loop", "eventid", "segid", "seghex", "segcode", "segname", "epoch", "pd"}


def validate_daterange_id_format(value: str) -> str:
    """Validate placeholders before writing the channel TOML."""
    if not isinstance(value, str):
        raise ValueError("daterange_id_format must be a string")
    try:
        for _literal, field_name, format_spec, conversion in string.Formatter().parse(value):
            if field_name is None:
                continue
            if field_name not in _DATERANGE_ID_FIELDS:
                raise ValueError(
                    f"unknown daterange_id_format placeholder {{{field_name}}}; "
                    f"supported: {', '.join(sorted(_DATERANGE_ID_FIELDS))}"
                )
            if format_spec or conversion:
                raise ValueError("daterange_id_format placeholders do not support format specs or conversions")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"invalid daterange_id_format: {exc}") from exc
    return value


def validate_epoch_utc(value: str) -> str:
    """The exact `YYYY-MM-DDTHH:MM:SSZ` form serve.py's --epoch-utc accepts."""
    try:
        datetime.datetime.strptime(value, _EPOCH_UTC_FORMAT)
    except (TypeError, ValueError):
        raise ValueError(
            f"epoch_utc must be an ISO8601 UTC timestamp like {DEFAULT_EPOCH_UTC}"
        ) from None
    return value


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
source_kind = "{source_kind}"
allow_missing_segments = {allow_missing_segments}
"""

_MARKERS_EXTRA = """
[markers]
daterange_mode      = "{daterange_mode}"
cue_tags            = "{cue_tags}"
increment_event_ids = {increment_event_ids}
daterange_id_format = "{daterange_id_format}"
dash_signal_format  = "{dash_signal_format}"
dash_descriptor_mode = "{dash_descriptor_mode}"
"""

_TIMESHIFT_EXTRA = """
[timeshift]
enabled          = {enabled}
start_param      = "{start_param}"
end_param        = "{end_param}"
max_span_seconds = {max_span_seconds}
"""

_ECS_EXPRESS_EXTRA = """
[packaging]
segment_duration   = {segment_duration}
dvr_window_seconds = {dvr_window_seconds}
hls_format = "{hls_format}"
hls_ts_mux_audio = {hls_ts_mux_audio}{period_extra}

[timeline]
epoch_utc = "{epoch_utc}"
continuous = {continuous}

[express]
port   = {port}
cpu    = {cpu}
memory = {memory}
"""

_LOCAL_DOCKER_EXTRA = """
[packaging]
segment_duration   = {segment_duration}
dvr_window_seconds = {dvr_window_seconds}
hls_format = "{hls_format}"
hls_ts_mux_audio = {hls_ts_mux_audio}{period_extra}

[timeline]
epoch_utc = "{epoch_utc}"
continuous = {continuous}

[docker]
port = {port}
"""


def _format_local_docker_port(port: int | str) -> str:
    """"auto" (the default) is a bare TOML string; an explicit port is a
    bare int -- see _local_docker_ops._resolve_port for how channel.py
    reads this back."""
    return '"auto"' if port == "auto" else str(int(port))


def generate_toml(
    *,
    name: str,
    backend: str,
    region: str,
    bucket_name: str,
    content_folder: str,
    source_path: str,
    source_kind: str = "playlist",
    allow_missing_segments: bool = False,
    segment_duration: float = 4.0,
    dvr_window_seconds: float = 30,
    epoch_utc: str = DEFAULT_EPOCH_UTC,
    hls_format: str = "cmaf",
    hls_ts_mux_audio: bool = True,
    continuous: bool = False,
    period_on_segmentation: list[str] | None = None,
    period_on_segmentation_apply: str = "both",
    timeshift_enabled: bool = True,
    timeshift_start_param: str = "start",
    timeshift_end_param: str = "end",
    timeshift_max_span_seconds: int = 21600,
    port: int | str = 8080,
    cpu: int = 256,
    memory: int = 512,
    daterange_mode: str = "shared",
    cue_tags: str = "none",
    increment_event_ids: bool = False,
    daterange_id_format: str = DEFAULT_DATERANGE_ID_FORMAT,
    dash_signal_format: str = "binary",
    dash_descriptor_mode: str = "shared",
) -> str:
    if backend not in ("aws-media", "ecs-express", "local-docker"):
        raise ValueError(
            f"backend must be 'aws-media', 'ecs-express', or 'local-docker', got {backend!r}"
        )
    if source_kind not in ("playlist", "archive", "manifest"):
        raise ValueError("source_kind must be 'playlist', 'archive' or 'manifest'")
    validate_daterange_id_format(daterange_id_format)
    validate_epoch_utc(epoch_utc)
    if hls_format not in ("cmaf", "ts"):
        raise ValueError("hls_format must be 'cmaf' or 'ts'")
    if source_kind not in ("playlist", "archive", "manifest"):
        raise ValueError("source_kind must be 'playlist', 'archive' or 'manifest'")
    if dash_signal_format not in ("binary", "xml"):
        raise ValueError("dash_signal_format must be 'binary' or 'xml'")
    if dash_descriptor_mode not in ("shared", "narrowed"):
        raise ValueError("dash_descriptor_mode must be 'shared' or 'narrowed'")
    if period_on_segmentation_apply not in ("both", "dash", "hls"):
        raise ValueError("period_on_segmentation_apply must be 'both', 'dash' or 'hls'")
    period_ids = [int(str(i), 0) for i in (period_on_segmentation or [])]
    if any(not 0 <= i <= 0xFF for i in period_ids):
        raise ValueError("period_on_segmentation entries must be segmentation_type_ids (0..255)")
    # loop-dee-loop/SCOPE.md §14 -- only written when something is selected.
    period_extra = (
        "\nperiod_on_segmentation = [" + ", ".join(f"0x{i:02X}" for i in period_ids) + "]"
        f'\nperiod_on_segmentation_apply = "{period_on_segmentation_apply}"'
        if period_ids else ""
    )

    def _timeshift_section() -> str:
        return _TIMESHIFT_EXTRA.format(
            enabled=str(timeshift_enabled).lower(),
            start_param=timeshift_start_param, end_param=timeshift_end_param,
            max_span_seconds=int(timeshift_max_span_seconds),
        )

    content = _TOML_TEMPLATE.format(
        name=name, backend=backend, region=region, bucket_name=bucket_name,
        content_folder=content_folder, source_path=source_path,
        source_kind=source_kind,
        allow_missing_segments=str(allow_missing_segments).lower(),
    )
    if backend == "ecs-express":
        if port == "auto":
            raise ValueError("port: 'auto' is only supported for the local-docker backend")
        content += _MARKERS_EXTRA.format(
            daterange_mode=daterange_mode, cue_tags=cue_tags,
            increment_event_ids=str(increment_event_ids).lower(),
            daterange_id_format=json.dumps(daterange_id_format, ensure_ascii=False)[1:-1],
            dash_signal_format=dash_signal_format,
            dash_descriptor_mode=dash_descriptor_mode,
        )
        content += _ECS_EXPRESS_EXTRA.format(
            segment_duration=segment_duration, dvr_window_seconds=dvr_window_seconds,
            epoch_utc=epoch_utc,
            hls_format=hls_format, hls_ts_mux_audio=str(hls_ts_mux_audio).lower(),
            continuous=str(continuous).lower(), period_extra=period_extra,
            port=int(port), cpu=cpu, memory=memory,
        )
        content += _timeshift_section()
    elif backend == "local-docker":
        # No [express] section -- local-docker has no Fargate CPU/memory
        # concept, and [aws]/[s3] above are written but ignored by
        # channel.py for this backend (kept for TOML-shape consistency).
        content += _MARKERS_EXTRA.format(
            daterange_mode=daterange_mode, cue_tags=cue_tags,
            increment_event_ids=str(increment_event_ids).lower(),
            daterange_id_format=json.dumps(daterange_id_format, ensure_ascii=False)[1:-1],
            dash_signal_format=dash_signal_format,
            dash_descriptor_mode=dash_descriptor_mode,
        )
        content += _LOCAL_DOCKER_EXTRA.format(
            segment_duration=segment_duration, dvr_window_seconds=dvr_window_seconds,
            epoch_utc=epoch_utc,
            hls_format=hls_format, hls_ts_mux_audio=str(hls_ts_mux_audio).lower(),
            continuous=str(continuous).lower(), period_extra=period_extra,
            port=_format_local_docker_port(port),
        )
        content += _timeshift_section()
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
    (create/spark/start/stop/refresh/update/redeploy/terminate) as a
    background Job."""
    if job_type not in ("create", "spark", "start", "stop", "refresh", "update", "redeploy", "terminate"):
        raise ValueError(f"Unsupported job_type: {job_type!r}")
    cmd = _channel_py_cmd([job_type, *(extra_args or [])], config_path=config_path, as_json=False)
    return runner.spawn(job_type, cmd, cwd=paths.ITS_A_LIVE_DIR, channel_name=channel_name)


def schedule_list(config_path: str) -> list[dict]:
    return _run_json(["schedule", "list"], config_path=config_path)


def schedule_remove(config_path: str, window_id: str) -> dict:
    return _run_json(["schedule", "remove", window_id], config_path=config_path)


def schedule_add_job(config_path: str, channel_name: str, start: str | None, end: str | None) -> Job:
    """Dispatched as a background Job (not run synchronously like
    schedule_list/schedule_remove above) because an immediate window
    (start=None) also runs `channel.py start` right after the EventBridge
    bookkeeping -- see channel.py's cmd_schedule -- which can take as long
    as the existing Start button's job already does."""
    extra: list[str] = ["add"]
    if start:
        extra += ["--start", start]
    if end:
        extra += ["--end", end]
    cmd = _channel_py_cmd(["schedule", *extra], config_path=config_path, as_json=False)
    return runner.spawn("schedule-add", cmd, cwd=paths.ITS_A_LIVE_DIR, channel_name=channel_name)
