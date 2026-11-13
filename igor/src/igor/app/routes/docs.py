"""Documentation hub: catalogued Markdown pages from the repo, plus the
channel API's OpenAPI spec rendered with ReDoc (the same page a running
channel serves at its own `/docs`)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

from igor.integrations import docs

router = APIRouter()


@router.get("/")
def list_docs() -> dict:
    return {"sections": docs.catalog()}


@router.get("/search")
def search_docs(q: str = Query("", max_length=200)) -> dict:
    """Sections of the catalogued pages containing every term in `q`, each with
    a highlighted snippet and the heading `anchor` to open it at."""
    return {"query": q, "results": docs.search(q)}


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


@router.get("/images/{path:path}")
def get_image(path: str) -> FileResponse:
    """Images referenced by catalogued pages, served from `docs/images/` only
    (png/jpg/gif/svg/webp; path-traversal guarded)."""
    root = docs.IMAGES_DIR.resolve()
    target = (root / path).resolve()
    if (
        root not in target.parents
        or target.suffix.lower() not in docs.IMAGE_SUFFIXES
        or not target.is_file()
    ):
        raise HTTPException(status_code=404, detail="Unknown documentation image.")
    return FileResponse(target)
