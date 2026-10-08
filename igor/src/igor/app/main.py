from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from igor.app.routes.archives import router as archives_router
from igor.app.routes.channels import router as channels_router
from igor.app.routes.docs import router as docs_router
from igor.app.routes.files import router as files_router
from igor.app.routes.jobs import router as jobs_router
from igor.app.routes.manifests import router as manifests_router
from igor.app.routes.playlists import router as playlists_router

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # igor/

app = FastAPI(
    title="Dr. Strangeloop",
    description=(
        "Define, launch, and monitor dr-strangeloop channels: "
        "franken-ts content/ad-break authoring, its-a-live deploy/lifecycle, "
        "and loop-dee-loop monitoring."
    ),
    # Under /api so one proxy rule (Vite dev, ingress) covers the API and its docs.
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.include_router(playlists_router, prefix="/api/v1/playlists", tags=["playlists"])
app.include_router(archives_router, prefix="/api/v1/archives", tags=["archives"])
app.include_router(manifests_router, prefix="/api/v1/manifests", tags=["manifests"])
app.include_router(channels_router, prefix="/api/v1/channels", tags=["channels"])
app.include_router(jobs_router, prefix="/api/v1/jobs", tags=["jobs"])
app.include_router(files_router, prefix="/api/v1/files", tags=["files"])
app.include_router(docs_router, prefix="/api/v1/docs", tags=["docs"])


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# Static SPA mount -- must be registered after the API routers above so
# /api/* is never shadowed by it. Silently absent if the frontend hasn't
# been built yet (see frontend/README.md / this project's README).
_admin_dist = PROJECT_ROOT / "frontend" / "dist"
if _admin_dist.is_dir():
    app.mount("/admin", StaticFiles(directory=str(_admin_dist), html=True), name="admin_ui")
