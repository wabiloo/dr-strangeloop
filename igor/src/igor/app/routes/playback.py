"""Playback test (player-lab): run a channel through several players headlessly."""

from __future__ import annotations

import mimetypes
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from igor.integrations import player_lab
from igor.jobs.runner import runner
from igor.store import channels as channel_store

router = APIRouter()
mimetypes.add_type("text/javascript", ".js")


class PlaybackTestPayload(BaseModel):
    players: list[str] | None = None
    formats: list[Literal["hls", "dash"]] | None = None
    boundaries: int = Field(default=2, ge=1, le=20)
    duration_s: int = Field(default=120, ge=10, le=3600, description="run length when the channel has no /timeline.json")
    max_seconds: int = Field(default=600, ge=30, le=3600)
    ffmpeg_s: int | None = Field(default=None, ge=5, le=600)


def _channel_config(name: str) -> str:
    try:
        channel_store.read_channel_config(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return channel_store.config_path_for(name)


@router.get("/info")
def playback_info() -> dict:
    """What this machine can run: players, SDKs installed, Chrome, ffmpeg."""
    try:
        return player_lab.info()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"player-lab unavailable: {exc}") from exc


@router.post("/setup")
def playback_setup() -> dict:
    return player_lab.spawn_setup().to_dict()


@router.get("/channels/{name}")
def list_playback_tests(name: str) -> dict:
    _channel_config(name)
    active = next((j for j in runner.list(channel_name=name)
                   if j.type == "playback-test" and j.status in ("queued", "running")), None)
    return {
        "runs": player_lab.list_runs(name),
        "active": None if active is None else {**active.to_dict(), "run_id": player_lab.run_id_of(active)},
    }


@router.post("/channels/{name}")
def start_playback_test(name: str, payload: PlaybackTestPayload) -> dict:
    config_path = _channel_config(name)
    if payload.players:
        known = player_lab.info()["players"]
        bad = [p for p in payload.players if p not in known]
        if bad:
            raise HTTPException(status_code=400, detail=f"unknown player(s) {bad}; known: {sorted(known)}")
    job, run_id = player_lab.spawn_run(
        name, config_path, players=payload.players, formats=payload.formats,
        boundaries=payload.boundaries, duration_s=payload.duration_s,
        max_seconds=payload.max_seconds, ffmpeg_s=payload.ffmpeg_s,
    )
    return {**job.to_dict(), "run_id": run_id}


@router.get("/channels/{name}/{run_id}")
def get_playback_test(name: str, run_id: str) -> dict:
    _channel_config(name)
    try:
        report = player_lab.read_report(name, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if report is None:
        raise HTTPException(status_code=404, detail="no report for this run (still running, or it failed before finishing)")
    return report


@router.get("/channels/{name}/{run_id}/{filename}")
def get_playback_screenshot(name: str, run_id: str, filename: str) -> FileResponse:
    _channel_config(name)
    path = player_lab.screenshot_path(name, run_id, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="no such screenshot")
    return FileResponse(path, media_type="image/png")


@router.get("/harness/{path:path}")
def get_harness_file(path: str) -> FileResponse:
    """The harness pages and player SDKs, served same-origin for the in-browser mode
    (the browser driver reads each player's `<video>` through an iframe)."""
    try:
        f = player_lab.harness_file(path or "browser.html")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"player-lab unavailable: {exc}") from exc
    if f is None:
        raise HTTPException(status_code=404, detail="not found (player SDKs installed?)")
    return FileResponse(f, media_type=mimetypes.guess_type(f.name)[0] or "application/octet-stream",
                        headers={"Cache-Control": "no-store"})


class BrowserResults(BaseModel):
    startedAt: str | None = None
    durationS: float | None = None
    userAgent: str | None = None
    target: dict[str, Any] = Field(default_factory=dict)
    boundaries: dict[str, Any] = Field(default_factory=dict)
    cases: list[dict[str, Any]] = Field(min_length=1)


@router.post("/channels/{name}/browser-results")
def post_browser_results(name: str, results: BrowserResults) -> dict:
    """Measurements from a run in the user's own browser; judged and stored like a headless run."""
    _channel_config(name)
    try:
        run_id, report = player_lab.judge_browser_run(name, results.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {**report, "run_id": run_id}
