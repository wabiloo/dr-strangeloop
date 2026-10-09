"""Playback-test routes: job dispatch and report reading (player-lab itself is stubbed)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from igor import paths
from igor.app.main import app
from igor.integrations import player_lab
from igor.jobs.runner import Job

client = TestClient(app)

CHANNEL = "break-and-ppos-short-local"  # ships in data/channels/


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "OUTPUTS_DIR", tmp_path)
    return tmp_path


def _write_report(outputs, run_id="20261009-101500", ok=True):
    d = player_lab.runs_root(CHANNEL) / run_id
    d.mkdir(parents=True)
    (d / "report.json").write_text(json.dumps({
        "generatedAt": "2026-10-09T10:15:00+00:00", "durationS": 42.0, "pass": ok,
        "cases": [{"player": "hlsjs", "format": "hls", "pass": ok}, {"player": "shaka", "format": "dash", "pass": True}],
    }))
    return d


def test_start_dispatches_job_with_out_dir(outputs, monkeypatch):
    seen = {}

    def fake_spawn(job_type, command, cwd, channel_name=None, env=None):
        seen.update(job_type=job_type, command=command, channel=channel_name)
        return Job(id="abc", type=job_type, channel_name=channel_name, command=command, cwd=str(cwd))

    monkeypatch.setattr(player_lab.runner, "spawn", fake_spawn)
    monkeypatch.setattr(player_lab, "info", lambda: {"players": {"hlsjs": ["hls"], "shaka": ["hls", "dash"]}})
    r = client.post(f"/api/v1/playback-test/channels/{CHANNEL}",
                    json={"players": ["hlsjs"], "formats": ["hls"], "boundaries": 3, "ffmpeg_s": 30})
    assert r.status_code == 200, r.text
    body = r.json()
    cmd = seen["command"]
    assert seen["job_type"] == "playback-test" and seen["channel"] == CHANNEL
    assert cmd[cmd.index("--players") + 1] == "hlsjs"
    assert cmd[cmd.index("--format") + 1] == "hls"
    assert cmd[cmd.index("--boundaries") + 1] == "3"
    assert cmd[cmd.index("--ffmpeg") + 1] == "30"
    assert cmd[cmd.index("--out-dir") + 1].endswith(f"channels/{CHANNEL}/{body['run_id']}")
    assert player_lab.run_id_of(Job(id="x", type="t", channel_name=None, command=cmd, cwd=".")) == body["run_id"]


def test_unknown_player_and_channel_rejected(outputs, monkeypatch):
    monkeypatch.setattr(player_lab, "info", lambda: {"players": {"hlsjs": ["hls"]}})
    assert client.post(f"/api/v1/playback-test/channels/{CHANNEL}", json={"players": ["nope"]}).status_code == 400
    assert client.post("/api/v1/playback-test/channels/does-not-exist", json={}).status_code == 404
    assert client.post(f"/api/v1/playback-test/channels/{CHANNEL}", json={"boundaries": 0}).status_code == 422


def test_list_and_read_reports(outputs):
    _write_report(outputs, "20261009-101500", ok=False)
    _write_report(outputs, "20261009-111500", ok=True)
    (player_lab.runs_root(CHANNEL) / "20261009-121500").mkdir()  # still running: no report yet
    runs = client.get(f"/api/v1/playback-test/channels/{CHANNEL}").json()["runs"]
    assert [r["run_id"] for r in runs] == ["20261009-111500", "20261009-101500"]
    assert runs[1]["passed"] is False and runs[1]["failed_cases"] == 1 and runs[1]["players"] == ["hlsjs", "shaka"]
    r = client.get(f"/api/v1/playback-test/channels/{CHANNEL}/20261009-101500")
    assert r.status_code == 200 and r.json()["pass"] is False
    assert client.get(f"/api/v1/playback-test/channels/{CHANNEL}/20261009-121500").status_code == 404


def test_run_id_and_screenshot_paths_are_validated(outputs):
    d = _write_report(outputs)
    (d / "hlsjs-hls.png").write_bytes(b"png")
    base = f"/api/v1/playback-test/channels/{CHANNEL}"
    assert client.get(f"{base}/..%2F..%2Fetc").status_code in (400, 404)
    assert client.get(f"{base}/not-a-run-id").status_code == 400
    assert client.get(f"{base}/20261009-101500/hlsjs-hls.png").status_code == 200
    assert client.get(f"{base}/20261009-101500/report.json").status_code == 404
    assert client.get(f"{base}/20261009-101500/..%2Freport.png").status_code == 404
