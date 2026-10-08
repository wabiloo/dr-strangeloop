from __future__ import annotations

from fastapi.testclient import TestClient

from igor.app.main import app
from igor.integrations import docs
from igor.paths import REPO_ROOT

client = TestClient(app)


def test_every_catalogued_page_exists():
    missing = [e.path for s in docs.CATALOG for e in s.entries if not (REPO_ROOT / e.path).is_file()]
    assert not missing


def test_catalog_slugs_and_paths_are_unique():
    entries = [e for s in docs.CATALOG for e in s.entries]
    assert len({e.slug for e in entries}) == len(entries)
    assert len({e.path for e in entries}) == len(entries)


def test_list_docs():
    resp = client.get("/api/v1/docs/")
    assert resp.status_code == 200
    slugs = {e["slug"] for s in resp.json()["sections"] for e in s["entries"]}
    assert {"overview", "cli-reference", "franken-ts-agents", "igor-agents"} <= slugs


def test_get_page_returns_markdown():
    resp = client.get("/api/v1/docs/pages/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == "docs/overview.md"
    assert body["markdown"].startswith("# Overview")


def test_unknown_page_is_404_and_paths_are_not_addressable():
    assert client.get("/api/v1/docs/pages/nope").status_code == 404
    assert client.get("/api/v1/docs/pages/..%2F..%2Fetc%2Fpasswd").status_code == 404


def test_channel_api_spec_and_viewer():
    spec = client.get("/api/v1/docs/channel-api/openapi.yaml")
    assert spec.status_code == 200
    assert "openapi:" in spec.text
    viewer = client.get("/api/v1/docs/channel-api/docs")
    assert viewer.status_code == 200
    assert 'spec-url="openapi.yaml"' in viewer.text


def test_igor_openapi_is_served_under_api():
    resp = client.get("/api/openapi.json")
    assert resp.status_code == 200
    assert "/api/v1/docs/pages/{slug}" in resp.json()["paths"]
    assert client.get("/api/docs").status_code == 200
