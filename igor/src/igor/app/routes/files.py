"""Local filesystem browsing + media probing (ffprobe) for the asset
file picker in the content editor."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from igor.integrations import files

router = APIRouter()

VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".ts", ".avi", ".webm", ".m3u8", ".mpd"}


@router.get("/browse")
def browse(path: str | None = Query(default=None)) -> dict:
    return files.browse_directory(path)


@router.get("/probe")
def probe(path_or_url: str = Query(...)) -> dict:
    try:
        return files.probe_media(path_or_url)
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/preview")
def preview(path: str = Query(...)) -> FileResponse:
    """Serve a local video source so the browser can play it in the picker."""
    try:
        media_path = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail="Video file not found.") from exc
    if not media_path.is_file():
        raise HTTPException(status_code=404, detail="Video file not found.")
    if media_path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise HTTPException(status_code=422, detail="Only video files can be previewed.")
    media_type = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
    return FileResponse(media_path, media_type=media_type)


@router.post("/upload")
async def upload(request: Request, filename: str = Query(...)) -> dict:
    """Store a browser-dropped asset where the backend can consume it."""
    try:
        return await files.save_uploaded_file(filename, request.stream())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
