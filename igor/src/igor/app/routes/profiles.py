"""Output profiles (encoding settings / ABR ladders): list, save, delete."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from igor.integrations import profiles

router = APIRouter()


class RenditionPayload(BaseModel):
    name: str
    resolution: str
    bitrate_kbps: int
    profile: str | None = None
    level: str | None = None


class ProfilePayload(BaseModel):
    mode: str
    resolution: str | None = None
    bitrate_kbps: int | None = None
    renditions: list[RenditionPayload] | None = None
    framerate: int
    gop: int | None = None
    profile: str | None = None
    level: str | None = None
    service_provider: str | None = None
    service_name: str | None = None


@router.get("/")
def list_profiles() -> list[dict]:
    return profiles.list_profiles()


@router.put("/{name}")
def save_profile(name: str, payload: ProfilePayload) -> dict:
    try:
        return profiles.save_profile(name, payload.model_dump())
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/{name}", status_code=204)
def delete_profile(name: str) -> None:
    try:
        profiles.delete_profile(name)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
