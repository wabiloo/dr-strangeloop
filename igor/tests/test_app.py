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
    assert schema["properties"]["enforce_scte35_marker_semantics"]["default"] is True


def test_list_playlists_returns_real_franken_ts_yaml_files():
    resp = client.get("/api/v1/playlists/")
    assert resp.status_code == 200
    playlists = {c["name"]: c for c in resp.json()}
    # These ship in the repo's data/playlists/ -- if this ever breaks,
    # either the fixtures moved or the list route regressed.
    assert "short-loop" in playlists
    assert playlists["short-loop"]["duration_seconds"] == 25
    assert playlists["short-loop"]["duration_estimated"] is False
    assert playlists["test1"]["duration_seconds"] == 180
    assert playlists["test1"]["duration_estimated"] is True


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


def test_preview_serves_local_video_and_rejects_non_video(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video bytes")
    image = tmp_path / "slate.png"
    image.write_bytes(b"image bytes")

    response = client.get("/api/v1/files/preview", params={"path": str(video)})
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert response.content == b"video bytes"

    response = client.get("/api/v1/files/preview", params={"path": str(image)})
    assert response.status_code == 422

    response = client.get("/api/v1/files/preview", params={"path": str(tmp_path / "missing.mp4")})
    assert response.status_code == 404


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


def _minimal_playlist(**overrides) -> dict:
    """A synthetic Config payload valid enough for pure model_validate
    (validate-markers never touches the filesystem, so file paths need not
    exist)."""
    payload = {
        "output": {"file": "outputs/test.ts"},
        "assets": [
            {"file": "content1.mp4", "duration": "10s"},
            {"file": "ad1.mp4", "id": "ad1", "duration": "5s"},
            {"file": "content2.mp4", "duration": "10s"},
        ],
        "markers": [
            {
                "event_id": 1,
                "splice_type": "time_signal",
                "assets": ["ad1"],
                "segmentation": {"type_id": "0x22"},
            },
        ],
    }
    payload.update(overrides)
    return payload


def test_validate_markers_returns_computed_numbering():
    resp = client.post("/api/v1/playlists/validate-markers", json={"data": _minimal_playlist()})
    assert resp.status_code == 200
    numbering = resp.json()["numbering"]
    # A single, unnumbered Break (no Program, break_numbering_supported
    # defaults false) -- franken-ts computes 0/0 for both Start and End.
    assert numbering == {"1": {"segment_num": 0, "segments_expected": 0, "sub_segment_num": None, "sub_segments_expected": None}}


def test_validate_markers_passes_through_authored_values_when_enforcement_off():
    payload = _minimal_playlist(enforce_scte35_marker_semantics=False)
    payload["markers"][0]["segmentation"]["segment_num"] = 7
    payload["markers"][0]["segmentation"]["segments_expected"] = 9
    resp = client.post("/api/v1/playlists/validate-markers", json={"data": payload})
    assert resp.status_code == 200
    assert resp.json()["numbering"]["1"]["segment_num"] == 7
    assert resp.json()["numbering"]["1"]["segments_expected"] == 9


def test_validate_markers_rejects_invalid_hierarchy():
    payload = _minimal_playlist()
    # A second Break over the exact same span as the first duplicates its
    # signal -- rejected by validate_markers's semantic checks.
    payload["markers"].append(
        {
            "event_id": 2,
            "splice_type": "time_signal",
            "assets": ["ad1"],
            "segmentation": {"type_id": "0x22"},
        }
    )
    resp = client.post("/api/v1/playlists/validate-markers", json={"data": payload})
    assert resp.status_code == 422


def _write_channel(configs_dir: Path, name: str, backend: str = "local-docker") -> None:
    (configs_dir / f"{name}.toml").write_text(f'[deploy]\nname = "{name}"\nbackend = "{backend}"\n')


def test_quick_channel_list_reads_configs_without_shelling_out(tmp_path, monkeypatch):
    from igor.integrations import its_a_live
    from igor.store import channels as channel_store

    monkeypatch.setattr(channel_store.paths, "ITS_A_LIVE_CONFIGS_DIR", tmp_path)
    _write_channel(tmp_path, "alpha")
    _write_channel(tmp_path, "beta", "ecs-express")
    (tmp_path / "broken.toml").write_text("not = [valid")
    (tmp_path / "nobackend.toml").write_text('[deploy]\nname = "x"\n')

    def boom(*a, **k):
        raise AssertionError("quick list must not call channel.py")

    monkeypatch.setattr(its_a_live, "list_channels", boom)
    resp = client.get("/api/v1/channels/?live=false")
    assert resp.status_code == 200
    rows = resp.json()
    assert [(r["name"], r["backend"]) for r in rows] == [("alpha", "local-docker"), ("beta", "ecs-express")]
    assert all("stack_status" not in r and "source_path" in r for r in rows)


def test_channel_summary_runs_channel_py_for_one_config(tmp_path, monkeypatch):
    from igor.integrations import its_a_live
    from igor.store import channels as channel_store

    monkeypatch.setattr(channel_store.paths, "ITS_A_LIVE_CONFIGS_DIR", tmp_path)
    _write_channel(tmp_path, "alpha")
    seen = []

    def fake_list(configs_dir=None):
        seen.append(configs_dir)
        return [{"config_path": configs_dir, "name": "alpha", "backend": "local-docker", "stack_status": "running"}]

    monkeypatch.setattr(its_a_live, "list_channels", fake_list)
    resp = client.get("/api/v1/channels/alpha/summary")
    assert resp.status_code == 200
    assert resp.json()["stack_status"] == "running"
    assert seen == [str(tmp_path / "alpha.toml")]

    assert client.get("/api/v1/channels/missing/summary").status_code == 404
    assert client.get("/api/v1/channels/..%2Fx/summary").status_code in (400, 404)
