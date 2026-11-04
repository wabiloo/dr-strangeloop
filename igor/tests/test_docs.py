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


# ── docs/low-latency.md: every relative link and anchor must resolve ──────────────────────────

import re  # noqa: E402


def _slug(heading: str) -> str:
    """GitHub-style heading anchor, as frontend/src/utils/markdown.ts `slugify` makes it."""
    text = re.sub(r"[`*]", "", heading).strip().lower()  # markup is not part of the rendered heading text
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s", "-", text)


def _anchors(path):
    return {_slug(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.+?)\s*$", path.read_text(encoding="utf-8"), re.MULTILINE)}


def test_the_low_latency_page_is_catalogued_and_its_links_resolve():
    page = REPO_ROOT / "docs" / "low-latency.md"
    assert page.is_file()
    assert docs.read_page("low-latency") is not None
    text = page.read_text(encoding="utf-8")
    catalogued = {e.path for s in docs.CATALOG for e in s.entries}
    bad = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        path_part, _, anchor = target.partition("#")
        dest = page if not path_part else (page.parent / path_part).resolve()
        if not dest.is_file():
            bad.append(f"{target}: no such file")
            continue
        if anchor and anchor not in _anchors(dest):
            bad.append(f"{target}: no heading '{anchor}' in {dest.name}")
        rel = dest.relative_to(REPO_ROOT.resolve()).as_posix()
        if path_part and rel not in catalogued and not rel.endswith((".yaml",)):
            bad.append(f"{target}: {rel} is not in the Docs menu (the viewer cannot open it)")
    assert not bad, "\n".join(bad)
