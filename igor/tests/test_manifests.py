"""VOD manifest-URL source routes/integration -- HTTP fetching is replaced by
a fake, and MANIFESTS_DIR/MANIFEST_IMPORTS_DIR point at a tmp tree so nothing
touches the real repo-root data/ or outputs/."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from igor.app.main import app
from igor.integrations import manifests

client = TestClient(app)

URL = "https://cdn.example.com/vod/movie/master.m3u8"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(manifests.paths, "MANIFESTS_DIR", tmp_path / "data" / "manifests")
    monkeypatch.setattr(manifests.paths, "MANIFEST_IMPORTS_DIR", tmp_path / "outputs" / "manifests")
    return tmp_path


@pytest.fixture()
def spawned(monkeypatch):
    calls = []

    class FakeJob:
        def to_dict(self):
            return {"id": "job1", "type": "manifest-import", "status": "queued"}

    def fake_spawn(job_type, command, cwd, channel_name=None, env=None):
        calls.append({"type": job_type, "command": command, "channel_name": channel_name})
        return FakeJob()

    monkeypatch.setattr(manifests.runner, "spawn", fake_spawn)
    return calls


def test_create_derives_a_name_from_the_url_and_lists_it(store):
    response = client.post("/api/v1/manifests/", json={"manifest_url": URL})

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "cdn.example.com_master"
    assert body["display_name"] == body["name"]
    assert body["manifest_url"] == URL and body["import"] is None
    assert [m["name"] for m in client.get("/api/v1/manifests/").json()] == ["cdn.example.com_master"]


def test_create_with_explicit_name_and_display_name_and_conflict(store):
    payload = {"manifest_url": URL, "name": "movie", "display_name": "The Movie"}
    assert client.post("/api/v1/manifests/", json=payload).json()["display_name"] == "The Movie"
    assert client.post("/api/v1/manifests/", json=payload).status_code == 409


@pytest.mark.parametrize("payload", [
    {"manifest_url": "ftp://x/y.m3u8"},
    {"manifest_url": "not a url"},
    {"manifest_url": URL, "name": "../etc"},
    {"manifest_url": URL, "name": "bad/name"},
])
def test_create_rejects_bad_input(store, payload):
    assert client.post("/api/v1/manifests/", json=payload).status_code == 422


def test_rename_and_delete(store):
    client.post("/api/v1/manifests/", json={"manifest_url": URL, "name": "movie"})

    assert client.put("/api/v1/manifests/movie/name", json={"display_name": "Renamed"}).json() == {
        "name": "movie", "display_name": "Renamed"}
    assert client.put("/api/v1/manifests/movie/name", json={"display_name": "  "}).status_code == 422
    assert client.delete("/api/v1/manifests/movie").status_code == 204
    assert client.get("/api/v1/manifests/movie").status_code == 404
    assert client.delete("/api/v1/manifests/movie").status_code == 404
    assert client.get("/api/v1/manifests/..%2Fx").status_code in (400, 404)


def test_inspect_returns_the_ladder_and_maps_failures(store, monkeypatch):
    ladder = {"format": "hls", "is_vod": True, "audio": True, "renditions": [
        {"position": 1, "name": "720p", "resolution": "1280x720", "bandwidth": 3_000_000}]}
    monkeypatch.setattr(manifests, "describe_manifest", lambda url: ladder)
    assert client.post("/api/v1/manifests/inspect", json={"manifest_url": URL}).json() == ladder

    def boom(url):
        raise RuntimeError("GET failed: 404")

    monkeypatch.setattr(manifests, "describe_manifest", boom)
    response = client.post("/api/v1/manifests/inspect", json={"manifest_url": URL})
    assert response.status_code == 502 and "404" in response.json()["detail"]
    assert client.post("/api/v1/manifests/inspect", json={"manifest_url": "file:///etc/passwd"}).status_code == 422


def test_import_spawns_ingest_url_with_the_chosen_options_and_remembers_them(store, spawned):
    client.post("/api/v1/manifests/", json={"manifest_url": URL, "name": "movie"})

    response = client.post("/api/v1/manifests/movie/import", json={
        "renditions": "#1,#3", "audio": False, "allow_missing_segments": True})

    assert response.status_code == 200
    command = spawned[0]["command"]
    assert command[command.index("ingest-url"):] == [
        "ingest-url", URL, "--output", str(store / "outputs" / "manifests" / "movie"),
        "--renditions", "#1,#3", "--no-audio", "--allow-missing-segments"]
    assert spawned[0]["type"] == "manifest-import" and spawned[0]["channel_name"] == "movie"
    assert client.get("/api/v1/manifests/movie").json()["import_options"] == {
        "renditions": "#1,#3", "audio": False, "allow_missing_segments": True}


def test_import_defaults_and_validation(store, spawned):
    client.post("/api/v1/manifests/", json={"manifest_url": URL, "name": "movie"})

    client.post("/api/v1/manifests/movie/import", json={})
    assert "--renditions" not in spawned[0]["command"] and "--no-audio" not in spawned[0]["command"]
    assert client.post("/api/v1/manifests/movie/import", json={"renditions": "720; rm -rf /"}).status_code == 422
    assert client.post("/api/v1/manifests/nope/import", json={}).status_code == 404


def test_import_status_summarises_the_written_manifest(store):
    client.post("/api/v1/manifests/", json={"manifest_url": URL, "name": "movie"})
    assert client.get("/api/v1/manifests/movie/import/status").json() == {
        "exists": False, "manifest_path": None, "summary": None}

    out = store / "outputs" / "manifests" / "movie"
    out.mkdir(parents=True)
    (out / "manifest.json").write_text(json.dumps({
        "segments": [{"index": 0, "duration_ticks": 360_000}, {"index": 1, "duration_ticks": 180_000}],
        "markers": [{}],
        "audio": {"separate": True},
        "renditions": [
            {"name": "720p", "variant": {"bandwidth": 3_000_000, "resolution": "1280x720"}, "media_files": []},
            {"name": "360p", "variant": {"bandwidth": 800_000}, "media_files": []},
        ],
    }))

    status = client.get("/api/v1/manifests/movie/import/status").json()
    assert status["exists"] is True and status["manifest_path"].endswith("movie/manifest.json")
    assert status["summary"] == {
        "segments": 2, "duration_seconds": 6.0, "markers": 1, "audio": True,
        "renditions": [
            {"name": "720p", "bandwidth": 3_000_000, "resolution": "1280x720"},
            {"name": "360p", "bandwidth": 800_000, "resolution": None}]}
    assert client.get("/api/v1/manifests/movie").json()["import"]["exists"] is True


def test_find_manifest_for_source(store):
    client.post("/api/v1/manifests/", json={"manifest_url": URL, "name": "movie"})

    assert manifests.find_manifest_for_source("/x/outputs/manifests/movie/manifest.json") == "movie"
    assert manifests.find_manifest_for_source("/x/outputs/manifests/gone/manifest.json") is None
    assert manifests.find_manifest_for_source("/x/outputs/archives/movie/manifest.json") is None
    assert manifests.find_manifest_for_source("") is None
