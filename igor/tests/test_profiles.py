"""Output profile routes -- PROFILES_DIR points at a tmp tree."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from igor.app.main import app
from igor.integrations import profiles

client = TestClient(app)

SINGLE = {"mode": "single", "resolution": "1280x720", "bitrate_kbps": 3000, "framerate": 25, "gop": 50}
LADDER = {
    "mode": "ladder",
    "renditions": [
        {"name": "720p", "resolution": "1280x720", "bitrate_kbps": 3000},
        {"name": "360p", "resolution": "640x360", "bitrate_kbps": 800},
    ],
    "framerate": 30,
    "service_provider": "acme",
}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles.paths, "PROFILES_DIR", tmp_path / "profiles")
    return tmp_path / "profiles"


def test_lists_builtins_first_and_flags_them(store):
    body = client.get("/api/v1/profiles/").json()
    assert [p["builtin"] for p in body] == [True, True]
    assert {p["mode"] for p in body} == {"single", "ladder"}
    ladder = next(p for p in body if p["mode"] == "ladder")
    assert [r["name"] for r in ladder["renditions"]] == ["232p", "360p", "480p", "720p", "1080p"]
    assert [(r["profile"], r["level"]) for r in ladder["renditions"]] == [
        ("baseline", "1.3"), ("main", "3.0"), ("main", "3.1"), ("high", "3.2"), ("high", "4.0"),
    ]


def test_save_lists_and_deletes_a_user_profile(store):
    assert client.put("/api/v1/profiles/my single", json=SINGLE).status_code == 200
    assert client.put("/api/v1/profiles/my ladder", json=LADDER).status_code == 200

    user = [p for p in client.get("/api/v1/profiles/").json() if not p["builtin"]]
    assert [p["name"] for p in user] == ["my ladder", "my single"]
    assert user[0]["service_provider"] == "acme"

    assert client.delete("/api/v1/profiles/my single").status_code == 204
    assert client.delete("/api/v1/profiles/my single").status_code == 404


def test_overwrite_replaces_the_profile(store):
    client.put("/api/v1/profiles/p", json=SINGLE)
    client.put("/api/v1/profiles/p", json={**SINGLE, "bitrate_kbps": 1234})
    user = [p for p in client.get("/api/v1/profiles/").json() if not p["builtin"]]
    assert len(user) == 1 and user[0]["bitrate_kbps"] == 1234


def test_builtins_cannot_be_overwritten_or_deleted(store):
    name = "1080p single, 5 Mbps"
    assert client.put(f"/api/v1/profiles/{name.lower()}", json=SINGLE).status_code == 403
    assert client.delete(f"/api/v1/profiles/{name}").status_code == 403


@pytest.mark.parametrize("body", [
    {**SINGLE, "mode": "weird"},
    {**SINGLE, "resolution": "big"},
    {**SINGLE, "bitrate_kbps": 0},
    {**SINGLE, "profile": "extended"},
    {**SINGLE, "level": "9.9"},
    {**LADDER, "renditions": [{**LADDER["renditions"][0], "level": "nope"}]},
    {"mode": "ladder", "framerate": 25, "renditions": []},
    {**LADDER, "renditions": LADDER["renditions"] + [LADDER["renditions"][0]]},
])
def test_invalid_profiles_are_rejected(store, body):
    assert client.put("/api/v1/profiles/bad", json=body).status_code == 422


def test_invalid_name_is_rejected(store):
    assert client.put("/api/v1/profiles/.hidden", json=SINGLE).status_code == 422


def test_h264_profile_and_level_round_trip(store):
    body = {**LADDER, "profile": "main", "level": "3.1"}
    body["renditions"] = [{**LADDER["renditions"][0], "profile": "baseline", "level": "3.0"}, LADDER["renditions"][1]]
    assert client.put("/api/v1/profiles/h264", json=body).status_code == 200
    saved = next(p for p in client.get("/api/v1/profiles/").json() if p["name"] == "h264")
    assert (saved["profile"], saved["level"]) == ("main", "3.1")
    assert (saved["renditions"][0]["profile"], saved["renditions"][0]["level"]) == ("baseline", "3.0")
    assert "profile" not in saved["renditions"][1]
