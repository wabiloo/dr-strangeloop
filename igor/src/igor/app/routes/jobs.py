"""Job status polling -- see jobs/runner.py for the execution model."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from igor.jobs.runner import runner

router = APIRouter()


@router.get("/")
def list_jobs(channel: str | None = Query(default=None)) -> list[dict]:
    return [job.to_dict(log_offset=0) for job in runner.list(channel_name=channel)]


@router.get("/{job_id}")
def get_job(job_id: str, log_offset: int = Query(default=0, ge=0)) -> dict:
    job = runner.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No such job: {job_id}")
    return job.to_dict(log_offset=log_offset)
