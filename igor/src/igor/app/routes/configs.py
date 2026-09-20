"""franken-ts content/ad-break config CRUD + build-job dispatch."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from igor.integrations import franken_ts

router = APIRouter()


class ConfigPayload(BaseModel):
    data: dict


@router.get("/schema")
def get_schema() -> dict:
    return franken_ts.config_schema()


@router.get("/")
def list_configs() -> list[dict]:
    return franken_ts.list_configs()


@router.get("/{name}")
def get_config(name: str) -> dict:
    try:
        return franken_ts.get_config(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/{name}")
def save_config(name: str, payload: ConfigPayload) -> dict:
    try:
        path = franken_ts.save_config(name, payload.data)
    except Exception as exc:  # noqa: BLE001 -- surface Pydantic validation errors to the client
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"path": path}


@router.delete("/{name}")
def delete_config(name: str) -> None:
    try:
        franken_ts.delete_config(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{name}/build")
def build_config(name: str) -> dict:
    try:
        job = franken_ts.spawn_build_job(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return job.to_dict()
