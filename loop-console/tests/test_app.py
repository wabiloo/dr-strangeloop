"""Smoke tests: the app assembles, and franken-ts config routes work
against real repo-root config files (no AWS/its-a-live calls -- those
need its-a-live's separate venv + credentials, out of scope here)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from loop_console.app.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_config_schema_is_derived_from_franken_ts():
    resp = client.get("/api/v1/configs/schema")
    assert resp.status_code == 200
    schema = resp.json()
    assert schema["title"] == "Config"
    assert "assets" in schema["properties"]


def test_list_configs_returns_real_franken_ts_yaml_files():
    resp = client.get("/api/v1/configs/")
    assert resp.status_code == 200
    names = {c["name"] for c in resp.json()}
    # These ship in the repo's franken-ts/configs/ -- if this ever breaks,
    # either the fixtures moved or the list route regressed.
    assert "short-loop" in names


def test_get_missing_config_is_404():
    resp = client.get("/api/v1/configs/does-not-exist")
    assert resp.status_code == 404
