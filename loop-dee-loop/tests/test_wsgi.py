"""wsgi.py is gunicorn's entrypoint (docker-entrypoint.sh's S3 / baked / lambda
paths). Nothing else imports it, so a broken import there only shows up as a
container that dies at boot ("Worker failed to boot"). Import it for real, with
a minimal package and the env vars the entrypoint sets."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_serve_continuity import _write_continuous_package  # noqa: E402


@pytest.fixture
def wsgi_module(tmp_path, monkeypatch):
    _write_continuous_package(tmp_path)
    monkeypatch.setenv("LOOP_PACKAGE_DIR", str(tmp_path))
    monkeypatch.setenv("EPOCH_UTC", "1970-01-01T00:00:00Z")
    monkeypatch.setenv("CONTINUOUS_TIMELINE", "true")
    monkeypatch.setenv("TIMESHIFT", "true")
    monkeypatch.setenv("QUERY_OVERRIDES", "true")
    monkeypatch.delenv("LOOP_PACKAGE_STORE", raising=False)
    sys.modules.pop("wsgi", None)
    module = importlib.import_module("wsgi")
    yield module
    sys.modules.pop("wsgi", None)


def test_wsgi_imports_and_builds_the_app(wsgi_module):
    assert wsgi_module.timeshift.timeshift is True
    assert wsgi_module.timeshift.overrides is True
    assert wsgi_module.continuous is True


def test_wsgi_app_serves_a_manifest(wsgi_module):
    response = wsgi_module.app.test_client().get("/index.m3u8")
    assert response.status_code == 200
    assert b"#EXTM3U" in response.data
