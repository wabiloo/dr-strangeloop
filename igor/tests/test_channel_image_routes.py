"""POST/GET /channels/{name}/image -- the shareable-image API (local-docker only)."""

from fastapi.testclient import TestClient

from igor.app.main import app
from igor.app.routes import channels as channels_routes
from igor.integrations import its_a_live

client = TestClient(app)


def _cfg(backend):
    return {"deploy": {"name": "chan", "backend": backend}}


def test_image_build_spawns_job_for_local_docker(monkeypatch):
    spawned = {}

    class FakeJob:
        def to_dict(self):
            return {"id": "j1", "type": "image"}

    def fake_spawn(job_type, config_path, name, extra_args=None):
        spawned.update(job_type=job_type, name=name, extra=extra_args)
        return FakeJob()

    monkeypatch.setattr(channels_routes.channel_store, "read_channel_config", lambda n: _cfg("local-docker"))
    monkeypatch.setattr(channels_routes.channel_store, "config_path_for", lambda n: f"/x/{n}.toml")
    monkeypatch.setattr(its_a_live, "spawn_job", fake_spawn)

    resp = client.post("/api/v1/channels/chan/image", json={"save": True, "epoch_utc": "2026-01-01T00:00:00Z"})
    assert resp.status_code == 200
    assert spawned == {
        "job_type": "image", "name": "chan",
        "extra": ["--epoch-utc", "2026-01-01T00:00:00Z", "--save"],
    }


def test_image_build_rejected_for_other_backends(monkeypatch):
    monkeypatch.setattr(channels_routes.channel_store, "read_channel_config", lambda n: _cfg("ecs-express"))
    resp = client.post("/api/v1/channels/chan/image", json={})
    assert resp.status_code == 400
    assert "local-docker" in resp.json()["detail"]


def test_image_download_404_until_exported(monkeypatch, tmp_path):
    monkeypatch.setattr(its_a_live, "exported_image_path", lambda n: tmp_path / f"{n}.tar")
    assert client.get("/api/v1/channels/chan/image").status_code == 404

    (tmp_path / "chan.tar").write_bytes(b"tarball")
    resp = client.get("/api/v1/channels/chan/image")
    assert resp.status_code == 200
    assert resp.content == b"tarball"
    assert "its-a-live-chan.tar" in resp.headers["content-disposition"]
