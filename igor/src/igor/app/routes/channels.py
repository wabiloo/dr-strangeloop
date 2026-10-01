"""its-a-live channel CRUD (define, deploy, start/stop/refresh, monitor)."""

from __future__ import annotations

import re

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator, model_validator

from igor.integrations import archives, franken_ts, its_a_live, manifests
from igor.integrations.its_a_live import (
    DEFAULT_DATERANGE_ID_FORMAT,
    DEFAULT_EPOCH_UTC,
    validate_daterange_id_format,
    validate_epoch_utc,
)
from igor.store import channels as channel_store

router = APIRouter()

# Channel name becomes a CloudFormation stack name component
# (ItsALiveStack-{name}-{backend}, see its-a-live/app.py), a bare TOML
# filename (data/channels/{name}.toml), and -- for local-docker/ecs-express
# -- a container/service name, so it needs to be a safe DNS-label-like
# token rather than just "non-empty".
_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
# A query-string parameter name that is safe to put unescaped in a URL and
# in CloudFront's cache-key allow-list.
_QUERY_PARAM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")


class ChannelCreatePayload(BaseModel):
    name: str
    backend: str  # "aws-media" | "ecs-express" | "local-docker"
    region: str
    bucket_name: str
    content_folder: str
    source_path: str
    # UI/source provenance metadata: archive segment-list inputs use source
    # segment durations rather than packaging.segment_duration.
    source_kind: str = "playlist"
    # grave-robber/SCOPE.md §10: [input].allow_missing_segments -- only
    # meaningful when source_path points at a grave-robber segment-list
    # manifest (.json) rather than a franken-ts .ts/rendition dir. Defaults
    # on for archive-import source-kind in ChannelNew.vue, but stays a
    # plain bake.py-level flag here (no backend distinction once a path is
    # picked -- see ChannelNew.vue's own comment).
    allow_missing_segments: bool = False
    segment_duration: float = 4.0
    dvr_window_seconds: float = 30
    # [packaging].epoch_utc -- loop 0's start / DASH availabilityStartTime
    # (ecs-express/local-docker). UTC, exactly YYYY-MM-DDTHH:MM:SSZ.
    epoch_utc: str = DEFAULT_EPOCH_UTC
    hls_format: str = "cmaf"
    hls_ts_mux_audio: bool = True
    # loop-dee-loop/SCOPE.md §12: default on -- serve.py rewrites each
    # segment's own timestamps per request (header patch, never a re-mux)
    # so the channel has no #EXT-X-DISCONTINUITY/DASH Period restart at the
    # loop wrap. ecs-express/local-docker only, same as hls_format/
    # hls_ts_mux_audio above.
    continuous_timeline: bool = False
    # loop-dee-loop/SCOPE.md §13: startover/catchup via query parameters on
    # the normal manifest URLs. Names are configurable per channel and
    # also drive CloudFront's manifest cache key (its-a-live/loop_stack.py).
    timeshift_enabled: bool = True
    timeshift_start_param: str = "start"
    timeshift_end_param: str = "end"
    timeshift_max_span_seconds: int = 21600
    # int to pin an explicit host port, "auto" (local-docker only) to let
    # it self-select a free one at start/refresh time -- see
    # its-a-live/AGENTS.md and _local_docker_ops._resolve_port.
    port: int | str = 8080
    cpu: int = 256
    memory: int = 512
    # [markers] -- shape of the HLS/DASH SCTE-35 signaling loop-dee-loop's
    # bake.py renders from this channel (see its-a-live/AGENTS.md).
    daterange_mode: str = "shared"
    cue_tags: str = "none"
    increment_event_ids: bool = False
    daterange_id_format: str = DEFAULT_DATERANGE_ID_FORMAT
    dash_signal_format: str = "binary"
    dash_descriptor_mode: str = "shared"

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(
                "name must be 1-63 lowercase letters, digits, or hyphens, "
                "and cannot start or end with a hyphen"
            )
        return v

    @field_validator("port")
    @classmethod
    def _validate_port(cls, v: int | str) -> int | str:
        if isinstance(v, str):
            if v != "auto":
                raise ValueError("port must be an integer, or the string 'auto' (local-docker only)")
            return v
        return v

    @field_validator("epoch_utc")
    @classmethod
    def _validate_epoch_utc(cls, v: str) -> str:
        return validate_epoch_utc(v)

    @field_validator("daterange_mode")
    @classmethod
    def _validate_daterange_mode(cls, v: str) -> str:
        if v not in ("grouped", "shared", "narrowed"):
            raise ValueError("daterange_mode must be 'grouped', 'shared', or 'narrowed'")
        return v

    @field_validator("cue_tags")
    @classmethod
    def _validate_cue_tags(cls, v: str) -> str:
        if v not in ("none", "alongside", "only"):
            raise ValueError("cue_tags must be 'none', 'alongside', or 'only'")
        return v

    @field_validator("hls_format")
    @classmethod
    def _validate_hls_format(cls, v: str) -> str:
        if v not in ("cmaf", "ts"):
            raise ValueError("hls_format must be 'cmaf' or 'ts'")
        return v

    @field_validator("source_kind")
    @classmethod
    def _validate_source_kind(cls, v: str) -> str:
        if v not in ("playlist", "archive", "manifest"):
            raise ValueError("source_kind must be 'playlist', 'archive' or 'manifest'")
        return v

    @field_validator("daterange_id_format")
    @classmethod
    def _validate_daterange_id_format(cls, v: str) -> str:
        return validate_daterange_id_format(v)

    @field_validator("dash_signal_format")
    @classmethod
    def _validate_dash_signal_format(cls, v: str) -> str:
        if v not in ("binary", "xml"):
            raise ValueError("dash_signal_format must be 'binary' or 'xml'")
        return v

    @field_validator("dash_descriptor_mode")
    @classmethod
    def _validate_dash_descriptor_mode(cls, v: str) -> str:
        if v not in ("shared", "narrowed"):
            raise ValueError("dash_descriptor_mode must be 'shared' or 'narrowed'")
        return v

    @field_validator(
        "timeshift_start_param", "timeshift_end_param",
    )
    @classmethod
    def _validate_timeshift_param_name(cls, v: str) -> str:
        if not _QUERY_PARAM_RE.match(v):
            raise ValueError(
                "timeshift parameter names must start with a letter or underscore and contain "
                "only letters, digits, '_', '-' or '.'"
            )
        return v

    @field_validator("timeshift_max_span_seconds")
    @classmethod
    def _validate_timeshift_max_span(cls, v: int) -> int:
        if v < 1:
            raise ValueError("timeshift_max_span_seconds must be >= 1")
        return v

    @model_validator(mode="after")
    def _validate_timeshift_names_distinct(self) -> "ChannelCreatePayload":
        # "full-loops", "timeline" and "offset" are fixed, non-configurable query params.
        names = [
            self.timeshift_start_param, self.timeshift_end_param,
            "full-loops", "timeline", "offset",
        ]
        if len(set(names)) != len(names):
            raise ValueError(f"timeshift parameter names must be distinct, got {names}")
        return self

    @model_validator(mode="after")
    def _validate_port_backend(self) -> "ChannelCreatePayload":
        if self.port == "auto" and self.backend != "local-docker":
            raise ValueError("port: 'auto' is only supported for the local-docker backend")
        return self


@router.get("/")
def list_channels() -> list[dict]:
    try:
        channels = its_a_live.list_channels(str(channel_store.paths.ITS_A_LIVE_CONFIGS_DIR))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Enrich each channel with its source identity. Playlist configs store
    # only the resolved output path, so those use a best-effort reverse
    # lookup; archive imports have a stable outputs/archives/<name> path.
    for channel in channels:
        source_path = ""
        source_kind = "playlist"
        try:
            cfg = channel_store.read_channel_config(channel["name"])
            source = cfg.get("input", {})
            source_path = source.get("source_path", "")
            source_kind = source.get("source_kind", "playlist")
        except FileNotFoundError:
            pass
        channel["source_path"] = source_path
        channel["playlist_name"] = franken_ts.find_playlist_for_source(source_path)
        channel["archive_name"] = archives.find_archive_for_source(source_path)
        channel["manifest_name"] = manifests.find_manifest_for_source(source_path)
        channel["manifest_display_name"] = (
            manifests.get_manifest(channel["manifest_name"])["display_name"] if channel["manifest_name"] else None
        )
        channel["source_kind"] = (
            "archive" if channel["archive_name"] else "manifest" if channel["manifest_name"] else source_kind
        )
        channel["archive_display_name"] = (
            archives.get_display_name(channel["archive_name"]) if channel["archive_name"] else None
        )

    return channels


@router.post("/")
def define_channel(payload: ChannelCreatePayload) -> dict:
    """Writes the its-a-live TOML config for a new channel. Does NOT touch
    AWS -- call POST /{name}/create afterwards to actually deploy it."""
    toml_content = its_a_live.generate_toml(**payload.model_dump())
    path = channel_store.write_channel_config(payload.name, toml_content)
    return {"path": path}


@router.put("/{name}")
def update_channel(name: str, payload: ChannelCreatePayload) -> dict:
    """Overwrites an existing channel's TOML config. Editable regardless
    of the channel's current lifecycle state (running/stopped/never
    deployed) -- there's no reason to require it be stopped first, since
    this only rewrites the local config file and never touches AWS or the
    local container/stack by itself. Call the appropriate action
    afterwards to actually apply the change to a deployed/running
    channel: `redeploy` (aws-media/ecs-express, or local-docker's
    container recreate) or `refresh`.

    `backend` cannot be changed here -- it drives the CloudFormation stack
    name / AWS resource identity (and local-docker's container name), so
    "changing" it is really defining a different channel. Delete and
    redefine instead if you need to switch backends. `name` in the body is
    ignored; the path segment is authoritative and channels cannot be
    renamed via PUT."""
    try:
        existing = channel_store.read_channel_config(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    existing_backend = existing.get("deploy", {}).get("backend")
    if payload.backend != existing_backend:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot change backend from {existing_backend!r} to {payload.backend!r} via edit "
                   f"-- delete and redefine the channel instead.",
        )

    data = payload.model_dump()
    data["name"] = name
    toml_content = its_a_live.generate_toml(**data)
    path = channel_store.write_channel_config(name, toml_content)
    return {"path": path}


@router.get("/{name}")
def get_channel(name: str) -> dict:
    try:
        return channel_store.read_channel_config(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{name}")
def delete_channel(name: str) -> None:
    """Removes the local TOML config only -- does NOT `cdk destroy` the
    stack. Run `channel.py redeploy`'s inverse (`cdk destroy`) yourself
    first if the channel is deployed; see its-a-live/AGENTS.md."""
    try:
        channel_store.delete_channel_config(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{name}/status")
def channel_status(name: str) -> dict:
    try:
        return its_a_live.get_status(channel_store.config_path_for(name))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/{name}/outputs")
def channel_outputs(name: str) -> dict:
    """local-docker has no CloudFormation stack, so no outputs -- returns
    an empty dict rather than erroring, so callers that always fetch
    /outputs alongside /status don't need a backend special-case."""
    try:
        cfg = channel_store.read_channel_config(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if cfg.get("deploy", {}).get("backend") == "local-docker":
        return {}
    try:
        return its_a_live.get_outputs(channel_store.config_path_for(name))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/{name}/health")
async def channel_health(name: str) -> dict:
    """Proxies loop-dee-loop's `serve.py` `/health` endpoint (ecs-express
    and local-docker channels only, both of which actually run serve.py --
    aws-media has no equivalent).

    - ecs-express: hits the direct ECS Express service endpoint (not the
      CloudFront domain) to bypass manifest/segment cache policies that
      weren't written with this path in mind.
    - local-docker: hits the container directly on localhost -- there's
      no stack/outputs to look up, just the configured port.
    """
    try:
        cfg = channel_store.read_channel_config(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    backend = cfg.get("deploy", {}).get("backend")

    if backend == "local-docker":
        # docker.port may be "auto" in the TOML -- ask channel.py for the
        # actually-resolved/running port (it reads it back off the
        # container itself) rather than reading the raw config value.
        status = its_a_live.get_status(channel_store.config_path_for(name))
        port = status.get("port")
        if not port:
            raise HTTPException(
                status_code=404,
                detail="No local-docker container running yet for this channel.",
            )
        url = f"http://localhost:{port}/health"
    elif backend == "ecs-express":
        outputs = its_a_live.get_outputs(channel_store.config_path_for(name))
        endpoint = outputs.get("ExpressServiceEndpoint")
        if not endpoint:
            raise HTTPException(
                status_code=404,
                detail="No ExpressServiceEndpoint output -- not deployed yet.",
            )
        url = endpoint.rstrip("/") + "/health"
    else:
        raise HTTPException(
            status_code=404,
            detail=f"backend {backend!r} has no /health endpoint (only ecs-express and local-docker run serve.py).",
        )

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        # Almost always means the channel isn't actually running yet (container
        # not started / task still spinning up / stopped) rather than a genuine
        # upstream gateway problem, so 503 (service unavailable) fits better
        # than 502 (bad gateway) -- and doesn't read like a proxy-layer bug in
        # server logs when polled every few seconds from the UI.
        raise HTTPException(status_code=503, detail=f"GET {url} failed: {exc}") from exc


def _spawn(job_type: str, name: str, extra_args: list[str] | None = None) -> dict:
    job = its_a_live.spawn_job(job_type, channel_store.config_path_for(name), name, extra_args)
    return job.to_dict()


@router.post("/{name}/create")
def create_channel(name: str) -> dict:
    """First-time deploy: ensure shared stack (ecs-express only), cdk
    deploy the channel stack, spark, start -- see channel.py's `create`
    command (added alongside this console)."""
    return _spawn("create", name)


@router.post("/{name}/spark")
def spark_channel(name: str) -> dict:
    return _spawn("spark", name)


@router.post("/{name}/start")
def start_channel(name: str) -> dict:
    return _spawn("start", name)


@router.post("/{name}/stop")
def stop_channel(name: str) -> dict:
    return _spawn("stop", name)


@router.post("/{name}/refresh")
def refresh_channel(name: str) -> dict:
    return _spawn("refresh", name)


@router.post("/{name}/update")
def update_channel_content(name: str) -> dict:
    """Spark then refresh in one step -- the routine "ship new content to
    a running channel" action (see channel.py's `update` command)."""
    return _spawn("update", name)


@router.post("/{name}/redeploy")
def redeploy_channel(name: str) -> dict:
    return _spawn("redeploy", name)


@router.post("/{name}/terminate")
def terminate_channel(name: str) -> dict:
    """Tears the CloudFormation stack down for good (`cdk destroy`) --
    see channel.py's `terminate` command. Not available for local-docker
    (no stack); `stop` alone is complete teardown there."""
    return _spawn("terminate", name)


class ScheduleWindowPayload(BaseModel):
    """Both optional: no `start` means the window begins immediately
    (this also starts the channel now); no `end` means it runs until a
    manual Stop. See channel.py's `schedule add` docstring."""
    start: str | None = None
    end: str | None = None


@router.get("/{name}/schedule")
def list_schedule_windows(name: str) -> list[dict]:
    """aws-media/ecs-express only -- see channel.py's `schedule list`.
    local-docker channels have no AWS presence to schedule against;
    channel.py itself refuses the command, which surfaces here as a 502."""
    try:
        return its_a_live.schedule_list(channel_store.config_path_for(name))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/{name}/schedule")
def add_schedule_window(name: str, payload: ScheduleWindowPayload) -> dict:
    """Spawned as a background Job -- see its_a_live.schedule_add_job's
    docstring for why (an immediate window also starts the channel)."""
    job = its_a_live.schedule_add_job(channel_store.config_path_for(name), name, payload.start, payload.end)
    return job.to_dict()


@router.delete("/{name}/schedule/{window_id}")
def remove_schedule_window(name: str, window_id: str) -> dict:
    """Only cancels the window's future EventBridge triggers -- does NOT
    stop the channel if the window is currently active (windows are
    automation triggers, not an enforced on/off state)."""
    try:
        return its_a_live.schedule_remove(channel_store.config_path_for(name), window_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
