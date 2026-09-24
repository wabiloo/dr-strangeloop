"""franken-ts content/ad-break playlist CRUD + build-job dispatch."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
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


@router.post("/{name}/report")
def build_report(name: str) -> dict:
    try:
        job = franken_ts.spawn_report_job(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return job.to_dict()


@router.get("/{name}/preview/status")
def get_preview_status(name: str) -> dict:
    """Whether a preview .mp4 exists and whether it's stale (older than
    the playlist YAML's mtime, i.e. rendered from a prior saved version)
    -- lets the Assemble tab hide the player after an edit+save without
    a fresh Assemble, without tracking that state client-side."""
    try:
        return franken_ts.preview_status(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{name}/output/status")
def get_output_status(name: str) -> dict:
    try:
        return franken_ts.output_status(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{name}/preview")
def get_preview(name: str) -> FileResponse:
    """Serves the quick 540p preview .mp4 franken-ts writes as the last
    step of a successful build, for the Assemble tab's <video> player.
    404s if the playlist has never been built (or was built before this
    feature existed)."""
    try:
        path = franken_ts.preview_mp4_path(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Preview not built yet -- run Assemble first.")
    return FileResponse(path, media_type="video/mp4")


@router.get("/{name}/report/status")
def get_report_status(name: str) -> dict:
    try:
        return franken_ts.report_status(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{name}/report")
def get_report(name: str) -> FileResponse:
    try:
        path = franken_ts.report_html_path(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Report not built yet -- run Assemble first.")
    return FileResponse(path, media_type="text/html")
