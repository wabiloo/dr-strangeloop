from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from loop_console.app.routes.channels import router as channels_router
from loop_console.app.routes.configs import router as configs_router
from loop_console.app.routes.jobs import router as jobs_router

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # loop-console/

app = FastAPI(
    title="loop-console",
    description=(
        "Define, launch, and monitor live-scte-loop-generator channels: "
        "franken-ts content/ad-break authoring, its-a-live deploy/lifecycle, "
        "and loop-dee-loop monitoring."
    ),
)

app.include_router(configs_router, prefix="/api/v1/configs", tags=["configs"])
app.include_router(channels_router, prefix="/api/v1/channels", tags=["channels"])
app.include_router(jobs_router, prefix="/api/v1/jobs", tags=["jobs"])


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# Static SPA mount -- must be registered after the API routers above so
# /api/* is never shadowed by it. Silently absent if the frontend hasn't
# been built yet (see frontend/README.md / this project's README).
_admin_dist = PROJECT_ROOT / "frontend" / "dist"
if _admin_dist.is_dir():
    app.mount("/admin", StaticFiles(directory=str(_admin_dist), html=True), name="admin_ui")
