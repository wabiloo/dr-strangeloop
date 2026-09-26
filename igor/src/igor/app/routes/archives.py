"""Archive-derived loop import: list/coverage/import-job-spawn/status --
mirrors playlists.py's shape (grave-robber/SCOPE.md §10's endpoint table)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from igor.integrations import archives

router = APIRouter()


class ImportPayload(BaseModel):
    manifest_url: str
    format: str | None = None
    start: str | None = None
    end: str | None = None


class SelectionPayload(BaseModel):
    manifest_url: str | None = None
    start: str | None = None
    end: str | None = None
    suggestion_id: str | None = None


class RenamePayload(BaseModel):
    display_name: str


@router.get("/")
def list_archives() -> list[dict]:
    return archives.list_archives()


@router.post("/upload")
async def upload_archive(
    request: Request,
    filename: str = Query(...),
    name: str | None = Query(default=None),
) -> dict:
    """Upload a capture, defaulting its stable ID from the filename."""
    try:
        return await archives.save_uploaded_archive(name, filename, request.stream())
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/{name}", status_code=204)
def delete_archive(name: str) -> None:
    try:
        archives.delete_archive(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/{name}/name")
def rename_archive(name: str, payload: RenamePayload) -> dict:
    try:
        return archives.rename_archive(name, payload.display_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
        job = archives.spawn_import_job(name, payload.manifest_url, format=payload.format, start=payload.start, end=payload.end)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return job.to_dict()


@router.get("/{name}/import/status")
def get_import_status(name: str) -> dict:
    try:
        return archives.import_status(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/{name}/selection")
def put_selection(name: str, payload: SelectionPayload) -> dict:
    try:
        return archives.save_selection(name, payload.model_dump())
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
