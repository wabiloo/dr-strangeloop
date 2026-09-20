"""Local filesystem browsing + media probing (ffprobe) for the asset
file picker in the content editor."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from loop_console.integrations import files

router = APIRouter()


@router.get("/browse")
def browse(path: str | None = Query(default=None)) -> dict:
    return files.browse_directory(path)


@router.get("/probe")
def probe(path_or_url: str = Query(...)) -> dict:
    try:
        return files.probe_media(path_or_url)
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
