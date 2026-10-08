"""Documentation hub: catalogued Markdown pages from the repo, plus the
channel API's OpenAPI spec rendered with ReDoc (the same page a running
channel serves at its own `/docs`)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from igor.integrations import docs

router = APIRouter()


@router.get("/")
def list_docs() -> dict:
    return {"sections": docs.catalog()}


@router.get("/channel-api/docs", response_class=HTMLResponse)
def channel_api_docs() -> HTMLResponse:
    """Static rendering of the channel API spec (no running channel needed).
    Its spec-url is relative, so it resolves to `./openapi.yaml` below."""
    return HTMLResponse(docs.channel_api_html())


@router.get("/channel-api/openapi.yaml")
def channel_api_spec() -> FileResponse:
    if not docs.CHANNEL_API_SPEC.is_file():
        raise HTTPException(status_code=404, detail="Channel API spec not found.")
    return FileResponse(docs.CHANNEL_API_SPEC, media_type="application/yaml")


@router.get("/pages/{slug}")
def get_page(slug: str) -> dict:
    page = docs.read_page(slug)
    if page is None:
        raise HTTPException(status_code=404, detail=f"Unknown documentation page {slug!r}.")
    return page
