"""its-a-live channel CRUD (define, deploy, start/stop/refresh, monitor)."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from igor.integrations import franken_ts, its_a_live
from igor.store import channels as channel_store

router = APIRouter()


class ChannelCreatePayload(BaseModel):
    name: str
    backend: str  # "aws-media" | "ecs-express"
    region: str
    bucket_name: str
    content_folder: str
    source_path: str
    segment_duration: float = 4.0
    dvr_window_seconds: float = 30
    port: int = 8080
    cpu: int = 256
    memory: int = 512


@router.get("/")
def list_channels() -> list[dict]:
    try:
        channels = its_a_live.list_channels(str(channel_store.paths.ITS_A_LIVE_CONFIGS_DIR))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Enrich with the franken-ts playlist each channel's [input].source_path
    # was (probably) built from -- channels only store the resolved output
    # path, not which playlist produced it, so this is a best-effort reverse
    # lookup (see find_playlist_for_source) purely for display in the list.
    for channel in channels:
        source_path = ""
        try:
            cfg = channel_store.read_channel_config(channel["name"])
            source_path = cfg.get("input", {}).get("source_path", "")
        except FileNotFoundError:
            pass
        channel["source_path"] = source_path
        channel["playlist_name"] = franken_ts.find_playlist_for_source(source_path)

    return channels


@router.post("/")
def define_channel(payload: ChannelCreatePayload) -> dict:
    """Writes the its-a-live TOML config for a new channel. Does NOT touch
    AWS -- call POST /{name}/create afterwards to actually deploy it."""
    toml_content = its_a_live.generate_toml(**payload.model_dump())
    path = channel_store.write_channel_config(payload.name, toml_content)
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
    try:
        return its_a_live.get_outputs(channel_store.config_path_for(name))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/{name}/health")
async def channel_health(name: str) -> dict:
    """Proxies loop-dee-loop's `serve.py` `/health` endpoint (ecs-express
    channels only) -- hits the direct ECS Express service endpoint (not
    the CloudFront domain) to bypass manifest/segment cache policies that
    weren't written with this path in mind."""
    outputs = its_a_live.get_outputs(channel_store.config_path_for(name))
    endpoint = outputs.get("ExpressServiceEndpoint")
    if not endpoint:
        raise HTTPException(
            status_code=404,
            detail="No ExpressServiceEndpoint output -- not an ecs-express channel, or not deployed yet.",
        )
    url = endpoint.rstrip("/") + "/health"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GET {url} failed: {exc}") from exc


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


@router.post("/{name}/redeploy")
def redeploy_channel(name: str) -> dict:
    return _spawn("redeploy", name)
