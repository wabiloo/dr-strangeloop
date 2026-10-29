"""Thin wrapper around its-a-live/channel.py.

its-a-live manages its own separate venv (aws-cdk-lib/boto3 kept isolated
from media tooling -- see repo AGENTS.md), so every call here shells out
to `channel.py` using that venv's Python (paths.its_a_live_python()) --
this module intentionally does not import boto3/its-a-live code directly.

Two kinds of operations:
- Fast, read-only ones (`list_channels`, `get_status`, `get_outputs`) are
  run synchronously (single AWS API call, <1s) and return parsed JSON.
- Long-running ones (`create`, `spark`, `start`, `stop`, `refresh`,
  `update`, `redeploy`, `terminate`, and local-docker's `image`) are dispatched as background Jobs
  (see jobs/runner.py) since they can block for minutes (cdk
  deploy/destroy, ECS Express canary, etc).
"""

from __future__ import annotations

import datetime
import json
import re
import subprocess
import string
from pathlib import Path

from igor import paths
from igor.jobs.runner import Job, runner

DEFAULT_DATERANGE_ID_FORMAT = "{segcode}-{eventid}-{loop}"
# Mirrors its-a-live/_epoch_cfg.py (that venv is separate, so not imported).
DEFAULT_EPOCH_UTC = "2026-01-01T00:00:00Z"
_EPOCH_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_DATERANGE_ID_FIELDS = {"loop", "eventid", "segid", "seghex", "segcode", "segname", "epoch", "pd"}
# The fixed-name query parameters of a channel's manifests. Mirrors
# its-a-live/_query_cfg.py (separate venv, so not imported; a test keeps the
# two in lockstep). `[query] start_param`/`end_param` must not collide with them.
QUERY_FIXED_PARAMS = (
    "full_loops", "timeline_mode", "offset", "epoch", "dvr_window",
    "timeline_style", "addressing", "cue_tags", "descriptor_grouping",
    "increment_event_ids", "signal_format", "id_template", "split_on",
    "max_height", "max_bitrate",
)
_QUERY_PREFIX_RE = re.compile(r"^[A-Za-z0-9_.~-]*$")


def validate_query_prefix(value: str) -> str:
    """`[query] prefix`: prepended to every query parameter name, so it must be URL-safe."""
    if not isinstance(value, str) or not _QUERY_PREFIX_RE.match(value):
        raise ValueError("query prefix must contain only letters, digits, '_', '-', '.' or '~'")
    return value


def validate_query_names(start_param: str, end_param: str, prefix: str = "") -> None:
    """Every on-the-wire name (prefix applied) must be distinct."""
    names = [f"{prefix}{n}" for n in (start_param, end_param, *QUERY_FIXED_PARAMS)]
    if len(set(names)) != len(names):
        raise ValueError(f"query parameter names must be distinct, got {names}")


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


# The layout of the channel TOML this module writes: its-a-live refuses a config
# of another version and migrates old ones (its-a-live/_legacy_cfg.py CONFIG_VERSION;
# a test keeps the two equal).
CONFIG_VERSION = 2

_TOML_TEMPLATE = """\
config_version = {config_version}

[deploy]
name = "{name}"
backend = "{backend}"

[infrastructure.aws]
region = "{region}"

[infrastructure.s3]
bucket_name = "{bucket_name}"
content_folder = "{content_folder}"

[input]
source_path = "{source_path}"
source_kind = "{source_kind}"
allow_missing_segments = {allow_missing_segments}
"""

_MARKERS_EXTRA = """
[markers]
increment_event_ids = {increment_event_ids}{split_on_extra}

[markers.hls]
descriptor_grouping = "{daterange_mode}"
cue_tags = "{cue_tags}"
id_template = "{daterange_id_format}"

[markers.dash]
descriptor_grouping = "{dash_descriptor_mode}"
signal_format = "{dash_signal_format}"
"""

_QUERY_EXTRA = """
[query]
timeshift = {timeshift}
overrides = {overrides}
prefix = "{prefix}"
start_param = "{start_param}"
end_param = "{end_param}"

[timeshift]
max_span_seconds = {max_span_seconds}
"""

_PACKAGING_EXTRA = """
[packaging]
segment_duration = {segment_duration}
dvr_window = {dvr_window_seconds}

[packaging.hls]
container = "{hls_format}"
mux_audio = {hls_ts_mux_audio}

[packaging.dash]
timeline_style = "{dash_timeline}"
addressing = "{dash_addressing}"

[timeline]
epoch = "{epoch_utc}"
continuous = {continuous}
"""

BACKENDS = ("aws-media", "ecs-express", "aws-lambda", "local-docker")

_ECS_EXPRESS_EXTRA = """
[infrastructure.express]
port   = {port}
cpu    = {cpu}
memory = {memory}
cdn    = {cdn}
"""

_AWS_LAMBDA_EXTRA = """
[infrastructure.lambda]
memory               = {memory}
timeout              = {timeout}
reserved_concurrency = {reserved_concurrency}
cache_mb             = {cache_mb}
architecture         = "{architecture}"
"""

_LOCAL_DOCKER_EXTRA = """
[infrastructure.docker]
port = {port}
bundle_content = {bundle_content}
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
    dash_addressing: str = "number",
    dash_timeline: str = "full",
    continuous: bool = False,
    period_on_segmentation: list[str] | None = None,
    timeshift_enabled: bool = True,
    query_overrides: bool = True,
    query_prefix: str = "",
    timeshift_start_param: str = "start",
    timeshift_end_param: str = "end",
    timeshift_max_span_seconds: int = 21600,
    port: int | str = 8080,
    bundle_content: bool = False,
    cpu: int = 256,
    memory: int = 512,
    cdn: bool = True,
    lambda_memory: int = 1024,
    lambda_timeout: int = 30,
    lambda_reserved_concurrency: int = 10,
    lambda_cache_mb: int = 512,
    lambda_architecture: str = "arm64",
    daterange_mode: str = "shared",
    cue_tags: str = "none",
    increment_event_ids: bool = False,
    daterange_id_format: str = DEFAULT_DATERANGE_ID_FORMAT,
    dash_signal_format: str = "binary",
    dash_descriptor_mode: str = "shared",
) -> str:
    if backend not in BACKENDS:
        raise ValueError(
            f"backend must be 'aws-media', 'ecs-express', 'aws-lambda' or 'local-docker', got {backend!r}"
        )
    if source_kind not in ("playlist", "archive", "manifest"):
        raise ValueError("source_kind must be 'playlist', 'archive' or 'manifest'")
    validate_daterange_id_format(daterange_id_format)
    validate_epoch_utc(epoch_utc)
    if bundle_content and backend != "local-docker":
        raise ValueError("bundle_content is only supported for the local-docker backend")
    if hls_format not in ("cmaf", "ts"):
        raise ValueError("hls_format must be 'cmaf' or 'ts'")
    if dash_addressing not in ("number", "time"):
        raise ValueError("dash_addressing must be 'number' or 'time'")
    if dash_timeline not in ("full", "compact"):
        raise ValueError("dash_timeline must be 'full' or 'compact'")
    if source_kind not in ("playlist", "archive", "manifest"):
        raise ValueError("source_kind must be 'playlist', 'archive' or 'manifest'")
    if dash_signal_format not in ("binary", "xml"):
        raise ValueError("dash_signal_format must be 'binary' or 'xml'")
    if dash_descriptor_mode not in ("shared", "narrowed"):
        raise ValueError("dash_descriptor_mode must be 'shared' or 'narrowed'")
    validate_query_prefix(query_prefix)
    validate_query_names(timeshift_start_param, timeshift_end_param, query_prefix)
    period_ids = [int(str(i), 0) for i in (period_on_segmentation or [])]
    if any(not 0 <= i <= 0xFF for i in period_ids):
        raise ValueError("period_on_segmentation entries must be segmentation_type_ids (0..255)")
    # loop-dee-loop/SCOPE.md §14 -- only written when something is selected.
    split_on_extra = (
        "\nsplit_on = [" + ", ".join(f"0x{i:02X}" for i in period_ids) + "]"
        if period_ids else ""
    )

    def _query_section() -> str:
        return _QUERY_EXTRA.format(
            timeshift=str(timeshift_enabled).lower(), overrides=str(query_overrides).lower(),
            prefix=query_prefix,
            start_param=timeshift_start_param, end_param=timeshift_end_param,
            max_span_seconds=int(timeshift_max_span_seconds),
        )

    def _markers_section() -> str:
        return _MARKERS_EXTRA.format(
            daterange_mode=daterange_mode, cue_tags=cue_tags,
            increment_event_ids=str(increment_event_ids).lower(),
            daterange_id_format=json.dumps(daterange_id_format, ensure_ascii=False)[1:-1],
            dash_signal_format=dash_signal_format,
            dash_descriptor_mode=dash_descriptor_mode,
            split_on_extra=split_on_extra,
        )

    def _packaging_section() -> str:
        return _PACKAGING_EXTRA.format(
            segment_duration=segment_duration, dvr_window_seconds=dvr_window_seconds,
            epoch_utc=epoch_utc,
            hls_format=hls_format, hls_ts_mux_audio=str(hls_ts_mux_audio).lower(),
            dash_addressing=dash_addressing, dash_timeline=dash_timeline,
            continuous=str(continuous).lower(),
        )

    content = _TOML_TEMPLATE.format(
        config_version=CONFIG_VERSION, name=name, backend=backend, region=region, bucket_name=bucket_name,
        content_folder=content_folder, source_path=source_path,
        source_kind=source_kind,
        allow_missing_segments=str(allow_missing_segments).lower(),
    )
    if backend == "ecs-express":
        if port == "auto":
            raise ValueError("port: 'auto' is only supported for the local-docker backend")
        content += _markers_section() + _packaging_section()
        content += _ECS_EXPRESS_EXTRA.format(port=int(port), cpu=cpu, memory=memory, cdn=str(cdn).lower())
        content += _query_section()
    elif backend == "aws-lambda":
        # No port: the Function URL / CloudFront own the endpoint.
        if lambda_architecture not in ("arm64", "x86_64"):
            raise ValueError("lambda_architecture must be 'arm64' or 'x86_64'")
        if lambda_reserved_concurrency == 0 or lambda_reserved_concurrency < -1:
            raise ValueError("lambda_reserved_concurrency must be >= 1, or -1 for no reservation")
        content += _markers_section() + _packaging_section()
        content += _AWS_LAMBDA_EXTRA.format(
            memory=int(lambda_memory), timeout=int(lambda_timeout),
            reserved_concurrency=int(lambda_reserved_concurrency), cache_mb=int(lambda_cache_mb),
            architecture=lambda_architecture,
        )
        content += _query_section()
    elif backend == "local-docker":
        # No [infrastructure.express] -- local-docker has no Fargate CPU/memory
        # concept, and [infrastructure.aws]/[infrastructure.s3] above are written but ignored by
        # channel.py for this backend (kept for TOML-shape consistency).
        content += _markers_section() + _packaging_section()
        content += _LOCAL_DOCKER_EXTRA.format(
            port=_format_local_docker_port(port),
            bundle_content=str(bundle_content).lower(),
        )
        content += _query_section()
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


def exported_image_path(channel_name: str) -> Path:
    """Where `channel.py image --save` (no explicit path) writes the
    channel's exported image tarball -- keep in sync with
    _local_docker_ops._image_export_dir()."""
    return Path(paths.OUTPUTS_DIR) / "images" / f"{channel_name}.tar"


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
    (create/spark/start/stop/refresh/update/redeploy/terminate/image) as a
    background Job."""
    if job_type not in ("create", "spark", "start", "stop", "refresh", "update", "redeploy", "terminate", "image"):
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
