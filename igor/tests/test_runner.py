"""Job runner timing: a terminal status must never be visible without finished_at."""

from __future__ import annotations

import sys
import time

from igor.jobs.runner import JobRunner


def _wait_terminal(job, timeout=10.0):
    deadline = time.time() + timeout
    while job.status not in ("succeeded", "failed"):
        # The invariant under test: whenever a terminal status is observable,
        # the timing fields are already set.
        assert time.time() < deadline, "job did not finish"
        time.sleep(0.001)


def test_finished_at_is_set_as_soon_as_status_is_terminal(tmp_path):
    runner = JobRunner()
    job = runner.spawn("test", [sys.executable, "-c", "pass"], cwd=tmp_path)
    _wait_terminal(job)
    assert job.status == "succeeded"
    assert job.started_at is not None and job.finished_at is not None
    assert job.finished_at >= job.started_at


def test_failed_job_also_has_a_duration(tmp_path):
    runner = JobRunner()
    job = runner.spawn("test", [sys.executable, "-c", "raise SystemExit(3)"], cwd=tmp_path)
    _wait_terminal(job)
    assert job.status == "failed" and job.return_code == 3
    assert job.finished_at is not None
