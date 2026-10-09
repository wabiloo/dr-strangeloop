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
    assert client.post(f"/api/v1/playback-test/channels/{CHANNEL}", json={"boundaries": -1}).status_code == 422


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


def test_harness_files_are_served_without_escaping_their_roots(tmp_path, monkeypatch):
    harness, vendor = tmp_path / "harness", tmp_path / "vendor"
    (harness / "adapters").mkdir(parents=True)
    (vendor / "hls.js").mkdir(parents=True)
    (harness / "browser.html").write_text("<html>page</html>")
    (harness / "adapters" / "x.js").write_text("export default 1")
    (vendor / "hls.js" / "hls.min.js").write_text("var Hls")
    (tmp_path / "secret.txt").write_text("nope")
    monkeypatch.setattr(player_lab, "asset_roots", lambda: (harness, vendor))

    base = "/api/v1/playback-test/harness"
    assert client.get(f"{base}/browser.html").text == "<html>page</html>"
    assert client.get(f"{base}/").status_code == 200  # defaults to the in-browser driver page
    assert client.get(f"{base}/adapters/x.js").headers["content-type"].startswith("text/javascript")
    assert client.get(f"{base}/vendor/hls.js/hls.min.js").text == "var Hls"
    assert client.get(f"{base}/vendor/../secret.txt").status_code == 404
    assert client.get(f"{base}/%2e%2e/secret.txt").status_code == 404
    assert client.get(f"{base}/missing.js").status_code == 404


def test_browser_results_are_judged_and_stored_under_the_channel(monkeypatch):
    seen = {}

    def fake_judge(channel, payload):
        seen.update(channel=channel, payload=payload)
        return "20261009-111111", {"pass": True, "cases": []}

    monkeypatch.setattr(player_lab, "judge_browser_run", fake_judge)
    body = {"cases": [{"player": "hlsjs", "format": "hls", "snapshot": {"startedAfterS": 1.2}}],
            "boundaries": {"requested": 1, "crossed": {"hls": 1}}}
    r = client.post(f"/api/v1/playback-test/channels/{CHANNEL}/browser-results", json=body)
    assert r.status_code == 200 and r.json()["run_id"] == "20261009-111111"
    assert seen["channel"] == CHANNEL and seen["payload"]["cases"][0]["player"] == "hlsjs"
    assert client.post("/api/v1/playback-test/channels/nope/browser-results", json=body).status_code == 404
    assert client.post(f"/api/v1/playback-test/channels/{CHANNEL}/browser-results", json={"cases": []}).status_code == 422
