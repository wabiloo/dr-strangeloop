"""VOD manifest-URL sources: list/create/inspect/import-job-spawn/status.
Separate from archives.py -- there is no capture, coverage map or range
picker here, just a URL, its rendition ladder, and a download job."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from igor.integrations import manifests

router = APIRouter()


class CreatePayload(BaseModel):
    manifest_url: str
    name: str | None = None
    display_name: str | None = None


class InspectPayload(BaseModel):
    manifest_url: str


class RenamePayload(BaseModel):
    display_name: str


class ImportPayload(BaseModel):
    renditions: str | None = None
    audio: bool = True
    allow_missing_segments: bool = False


@router.get("/")
def list_manifests() -> list[dict]:
    return manifests.list_manifests()


@router.post("/", status_code=201)
def create_manifest(payload: CreatePayload) -> dict:
    try:
        return manifests.create_manifest(payload.manifest_url, payload.name, payload.display_name)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/inspect")
def inspect_manifest(payload: InspectPayload) -> dict:
    try:
        return manifests.inspect_manifest(payload.manifest_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 -- fetch/parse failures are the client's to see
        raise HTTPException(status_code=502, detail=f"Could not read the manifest: {exc}") from exc


@router.get("/{name}")
def get_manifest(name: str) -> dict:
    try:
        return manifests.get_manifest(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{name}", status_code=204)
def delete_manifest(name: str) -> None:
    try:
        manifests.delete_manifest(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/{name}/name")
def rename_manifest(name: str, payload: RenamePayload) -> dict:
    try:
        return manifests.rename_manifest(name, payload.display_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{name}/import")
def spawn_import(name: str, payload: ImportPayload) -> dict:
    try:
        job = manifests.spawn_import_job(
            name, renditions=payload.renditions, audio=payload.audio,
            allow_missing_segments=payload.allow_missing_segments,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return job.to_dict()


@router.get("/{name}/import/status")
def get_import_status(name: str) -> dict:
    try:
        return manifests.import_status(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
