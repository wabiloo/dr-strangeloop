"""franken-ts content/ad-break playlist CRUD + build-job dispatch."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from igor.integrations import franken_ts

router = APIRouter()


class PlaylistPayload(BaseModel):
    data: dict


class DuplicatePlaylistPayload(BaseModel):
    new_name: str


@router.get("/schema")
def get_schema() -> dict:
    return franken_ts.playlist_schema()


@router.get("/")
def list_playlists() -> list[dict]:
    return franken_ts.list_playlists()


@router.get("/{name}")
def get_playlist(name: str) -> dict:
    try:
        return franken_ts.get_playlist(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/{name}")
def save_playlist(name: str, payload: PlaylistPayload) -> dict:
    try:
        path = franken_ts.save_playlist(name, payload.data)
    except Exception as exc:  # noqa: BLE001 -- surface Pydantic validation errors to the client
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"path": path}


@router.delete("/{name}")
def delete_playlist(name: str) -> None:
    try:
        franken_ts.delete_playlist(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{name}/duplicate")
def duplicate_playlist(name: str, payload: DuplicatePlaylistPayload) -> dict:
    try:
        path = franken_ts.duplicate_playlist(name, payload.new_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 -- surface Pydantic validation / bad-name errors to the client
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"path": path}


@router.post("/{name}/resolve-markers")
def resolve_markers(name: str, payload: PlaylistPayload | None = None) -> dict:
    """Probe real source files and resolve `markers` spans/segment_num for
    the timeline editor -- used to render marker lanes and auto-fill IDs
    without requiring a full build. Pass a body ({"data": ...}) to preview
    unsaved edits; omit it to resolve the saved-on-disk playlist."""
    try:
        return franken_ts.resolve_markers_preview(name, payload.data if payload else None)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 -- surface validation/probe errors to the client
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{name}/build")
def build_playlist(name: str) -> dict:
    try:
        job = franken_ts.spawn_build_job(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return job.to_dict()
