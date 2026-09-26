"""Archive-derived loop import: list/coverage/import-job-spawn/status --
mirrors playlists.py's shape (grave-robber/SCOPE.md §10's endpoint table)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from igor.integrations import archives

router = APIRouter()


class ImportPayload(BaseModel):
    manifest_url: str
    format: str | None = None


@router.get("/")
def list_archives() -> list[dict]:
    return archives.list_archives()


@router.get("/{name}/coverage")
def get_coverage(name: str) -> dict:
    try:
        return archives.get_coverage(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 -- surface parse/coverage errors to the client
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{name}/import")
def spawn_import(name: str, payload: ImportPayload) -> dict:
    try:
        job = archives.spawn_import_job(name, payload.manifest_url, format=payload.format)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return job.to_dict()


@router.get("/{name}/import/status")
def get_import_status(name: str) -> dict:
    try:
        return archives.import_status(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
