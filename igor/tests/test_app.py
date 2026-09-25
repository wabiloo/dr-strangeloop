"""Smoke tests: the app assembles, and franken-ts playlist routes work
against real repo-root playlist files (no AWS/its-a-live calls -- those
need its-a-live's separate venv + credentials, out of scope here)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from igor.app.main import app
from igor.paths import REPO_ROOT

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_playlist_schema_is_derived_from_franken_ts():
    resp = client.get("/api/v1/playlists/schema")
    assert resp.status_code == 200
    schema = resp.json()
    assert schema["title"] == "Config"
    assert "assets" in schema["properties"]


def test_list_playlists_returns_real_franken_ts_yaml_files():
    resp = client.get("/api/v1/playlists/")
    assert resp.status_code == 200
    names = {c["name"] for c in resp.json()}
    # These ship in the repo's data/playlists/ -- if this ever breaks,
    # either the fixtures moved or the list route regressed.
    assert "short-loop" in names


def test_get_missing_playlist_is_404():
    resp = client.get("/api/v1/playlists/does-not-exist")
    assert resp.status_code == 404


def test_duplicate_playlist_creates_a_copy_and_cleans_up():
    new_name = "short-loop-duplicate-test"
    resp = client.post(f"/api/v1/playlists/short-loop/duplicate", json={"new_name": new_name})
    try:
        assert resp.status_code == 200
        original = client.get("/api/v1/playlists/short-loop").json()
        copy = client.get(f"/api/v1/playlists/{new_name}").json()
        assert copy == original
    finally:
        client.delete(f"/api/v1/playlists/{new_name}")
    assert client.get(f"/api/v1/playlists/{new_name}").status_code == 404


def test_duplicate_playlist_conflict_when_target_exists():
    resp = client.post("/api/v1/playlists/short-loop/duplicate", json={"new_name": "short-loop"})
    assert resp.status_code == 409


def test_duplicate_missing_playlist_is_404():
    resp = client.post("/api/v1/playlists/does-not-exist/duplicate", json={"new_name": "whatever"})
    assert resp.status_code == 404


def test_browse_files_lists_a_real_directory():
    resp = client.get("/api/v1/files/browse", params={"path": str(REPO_ROOT / "data" / "playlists")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"].endswith("data/playlists")
    names = {e["name"] for e in body["entries"]}
    assert "short-loop.yaml" in names


def test_probe_missing_file_is_422():
    resp = client.get("/api/v1/files/probe", params={"path_or_url": "/no/such/file.mp4"})
    assert resp.status_code == 422


def test_upload_file_stores_backend_local_copy(tmp_path, monkeypatch):
    from igor import paths

    monkeypatch.setattr(paths, "ASSET_UPLOADS_DIR", tmp_path)
    resp = client.post(
        "/api/v1/files/upload",
        params={"filename": "../../movie clip.mp4"},
        content=b"test media bytes",
    )
    assert resp.status_code == 200
    result = resp.json()
    uploaded = tmp_path / Path(result["path"]).name
    assert uploaded.is_file()
    assert uploaded.read_bytes() == b"test media bytes"
    assert result["name"] == "movie_clip.mp4"
