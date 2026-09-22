"""In-memory async job runner.

Every long-running operation this console triggers (franken-ts build,
`cdk deploy`, `channel.py create/spark/start/stop/refresh/redeploy`) is a
subprocess that can take anywhere from seconds to ~15 minutes and only
produces human-readable stdout/stderr today (see igor's design
notes / the repo's AGENTS.md family for why: none of the underlying tools
have a structured progress API). Rather than block an HTTP request for
that long, every such operation is wrapped in a `Job`: a background
thread runs the subprocess, appends output line-by-line to the job's log,
and the frontend polls `GET /api/v1/jobs/{id}` until it's done.

Single-process, in-memory only (matches cue-graft's "keep it simple,
single instance" philosophy) -- job history does not survive a backend
restart. If that becomes a problem, swap `_JOBS`/lock for a small SQLite
table without changing the public interface.
"""

from __future__ import annotations

import itertools
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

JobStatus = Literal["queued", "running", "succeeded", "failed"]


@dataclass
class Job:
    id: str
    type: str
    channel_name: str | None
    command: list[str]
    cwd: str
    status: JobStatus = "queued"
    log: list[str] = field(default_factory=list)
    return_code: int | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def to_dict(self, log_offset: int = 0) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "channel_name": self.channel_name,
            "command": self.command,
            "status": self.status,
            "log": self.log[log_offset:],
            "log_length": len(self.log),
            "return_code": self.return_code,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class JobRunner:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._counter = itertools.count(1)

    def spawn(
        self,
        job_type: str,
        command: list[str],
        cwd: Path | str,
        channel_name: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], type=job_type, channel_name=channel_name,
                   command=command, cwd=str(cwd))
        with self._lock:
            self._jobs[job.id] = job
        thread = threading.Thread(
            target=self._run, args=(job, env), daemon=True, name=f"job-{job.id}"
        )
        thread.start()
        return job

    def _run(self, job: Job, env: dict[str, str] | None) -> None:
        import os

        job.status = "running"
        job.started_at = time.time()
        try:
            full_env = {**os.environ, **(env or {})}
            # franken-ts uses `rich` for its console output; when stdout is a
            # pipe (not a real TTY, as it always is here), rich falls back to
            # a hardcoded 80-column width, causing tables/lines to wrap and
            # truncate ("..."). Set a wider COLUMNS so output is legible in
            # igor's log panel.
            full_env.setdefault("COLUMNS", "145")
            proc = subprocess.Popen(
                job.command,
                cwd=job.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=full_env,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                job.log.append(line.rstrip("\n"))
            proc.wait()
            job.return_code = proc.returncode
            job.status = "succeeded" if proc.returncode == 0 else "failed"
        except Exception as exc:  # noqa: BLE001 -- surface any launch failure into the job log
            job.log.append(f"igor: failed to run job: {exc!r}")
            job.status = "failed"
            job.return_code = -1
        finally:
            job.finished_at = time.time()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self, channel_name: str | None = None) -> list[Job]:
        jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        if channel_name is not None:
            jobs = [j for j in jobs if j.channel_name == channel_name]
        return jobs


# Process-wide singleton -- fine for a single-instance deployment (see module docstring).
runner = JobRunner()
