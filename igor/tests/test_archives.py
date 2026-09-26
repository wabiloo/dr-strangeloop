"""Archive-derived loop import routes/integration (grave-robber/SCOPE.md
§10) -- against a real small HAR fixture (shared with grave-robber's own
test suite, not duplicated) and a monkeypatched ARCHIVES_DIR/
ARCHIVE_IMPORTS_DIR so nothing touches the real repo-root data/ tree."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from igor.app.main import app
from igor.integrations import archives

client = TestClient(app)

GRAVE_ROBBER_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "grave-robber" / "tests" / "fixtures" / "trace-shrink" / "hls1-chrome.har"
)
MEDIA_PLAYLIST_URL = (
    "https://stream.broadpeak.io/hls/live/2003678/ndtv24x7/masterp_720p@3.m3u8"
    "?bpkio_serviceid=300d1539c3b6aa17a79a8fd9f1e45448"
    "&bpkio_sessionid=10a0d36d4-f64af804-55e5-4e25-8b94-119d626d0bf2"
    "&category=all&mm_sp"
)


@pytest.fixture()
def archive_store(tmp_path, monkeypatch):
    archives_dir = tmp_path / "archives"
    imports_dir = tmp_path / "outputs" / "archives"
    archives_dir.mkdir(parents=True)
    monkeypatch.setattr(archives.paths, "ARCHIVES_DIR", archives_dir)
    monkeypatch.setattr(archives.paths, "ARCHIVE_IMPORTS_DIR", imports_dir)
    if GRAVE_ROBBER_FIXTURE.exists():
        shutil.copy(GRAVE_ROBBER_FIXTURE, archives_dir / "hls1-chrome.har")
    return archives_dir


def test_list_archives_empty_store_creates_dir_and_returns_empty(tmp_path, monkeypatch):
    archives_dir = tmp_path / "archives"
    monkeypatch.setattr(archives.paths, "ARCHIVES_DIR", archives_dir)

    result = archives.list_archives()

    assert result == []
    assert archives_dir.is_dir()


def test_list_archives_reports_metadata_for_a_real_har(archive_store):
    if not GRAVE_ROBBER_FIXTURE.exists():
        pytest.skip("grave-robber HAR fixture not found")

    result = archives.list_archives()

    assert len(result) == 1
    entry = result[0]
    assert entry["name"] == "hls1-chrome"
    assert entry["format"] == "har"
    assert entry["variant_count"] == 3
    assert entry["import"] is None  # nothing imported yet


def test_get_coverage_reports_every_variant(archive_store):
    if not GRAVE_ROBBER_FIXTURE.exists():
        pytest.skip("grave-robber HAR fixture not found")

    coverage = archives.get_coverage("hls1-chrome")

    assert coverage["name"] == "hls1-chrome"
    # The fixture's multivariant playlist is reported as info, not as an importable variant.
    assert len(coverage["variants"]) == 2
    assert len(coverage["multivariants"]) == 1
    assert all(v["format"] == "HLS" for v in coverage["variants"])


def test_get_coverage_unknown_archive_raises():
    with pytest.raises(FileNotFoundError):
        archives.get_coverage("does-not-exist")


def test_import_status_before_and_after_import(archive_store, monkeypatch):
    if not GRAVE_ROBBER_FIXTURE.exists():
        pytest.skip("grave-robber HAR fixture not found")

    status = archives.import_status("hls1-chrome")
    assert status == {"exists": False, "stale": True, "manifest_path": None}

    # Simulate a completed import without actually spawning a subprocess.
    output_dir = archives._import_output_dir("hls1-chrome")
    output_dir.mkdir(parents=True)
    (output_dir / "manifest.json").write_text('{"segments": [], "markers": []}')

    status = archives.import_status("hls1-chrome")
    assert status["exists"] is True
    assert status["stale"] is False


def test_spawn_import_job_builds_expected_command(archive_store, monkeypatch):
    if not GRAVE_ROBBER_FIXTURE.exists():
        pytest.skip("grave-robber HAR fixture not found")
    captured = {}

    class _FakeJob:
        def to_dict(self):
            return {"id": "fake"}

    def _fake_spawn(job_type, command, cwd, channel_name=None):
        captured["job_type"] = job_type
        captured["command"] = command
        captured["channel_name"] = channel_name
        return _FakeJob()

    monkeypatch.setattr(archives.runner, "spawn", _fake_spawn)

    job = archives.spawn_import_job("hls1-chrome", MEDIA_PLAYLIST_URL)

    assert job.to_dict() == {"id": "fake"}
    assert captured["job_type"] == "archive-import"
    assert captured["channel_name"] == "hls1-chrome"
    assert "ingest" in captured["command"]
    assert str(archive_store / "hls1-chrome.har") in captured["command"]
    assert MEDIA_PLAYLIST_URL in captured["command"]


def test_spawn_import_job_unknown_archive_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(archives.paths, "ARCHIVES_DIR", tmp_path / "archives")
    (tmp_path / "archives").mkdir()

    with pytest.raises(FileNotFoundError):
        archives.spawn_import_job("does-not-exist", MEDIA_PLAYLIST_URL)


# ── routes (FastAPI TestClient) ──────────────────────────────────────────


def test_route_list_archives(archive_store):
    resp = client.get("/api/v1/archives/")
    assert resp.status_code == 200


def test_route_coverage_404_for_unknown_archive(archive_store):
    resp = client.get("/api/v1/archives/does-not-exist/coverage")
    assert resp.status_code == 404


def test_route_import_status_for_unknown_archive_is_not_imported(archive_store):
    resp = client.get("/api/v1/archives/does-not-exist/import/status")
    assert resp.status_code == 200
    assert resp.json()["exists"] is False
