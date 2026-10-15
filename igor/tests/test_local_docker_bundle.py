"""local-docker `bundle_content`: volume mount (default) vs. loop package
baked into a per-channel image. No Docker needed -- subprocess is faked and
the commands it would have run are asserted on."""

import json
import os
import subprocess
import sys

import pytest

ITS_A_LIVE = os.path.join(os.path.dirname(__file__), "..", "..", "its-a-live")
sys.path.insert(0, os.path.abspath(ITS_A_LIVE))

import _local_docker_ops as ops  # noqa: E402


class FakeDocker:
    def __init__(self, container_status=None):
        self.calls = []
        self.container_status = container_status

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        out = ""
        if cmd[:2] == ["docker", "save"]:
            with open(cmd[cmd.index("-o") + 1], "wb") as f:
                f.write(b"tar")
        elif cmd[:3] == ["docker", "image", "inspect"]:
            out = "sha256:fake\n" if "{{.Id}}" in cmd else "1048576\n"
        elif cmd[:2] == ["docker", "inspect"]:
            if self.container_status is None:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
            out = f"{self.container_status}\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    def runs(self):
        return [c for c in self.calls if c[:2] == ["docker", "run"]]

    def builds(self):
        return [c for c in self.calls if c[:2] == ["docker", "build"]]


@pytest.fixture
def env(tmp_path, monkeypatch):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "loop_descriptor.json").write_text("{}")
    fake = FakeDocker()
    monkeypatch.setattr(ops.subprocess, "run", fake)
    monkeypatch.setattr(ops.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(ops, "_image_export_dir", lambda: str(tmp_path / "images"))
    return package, fake


def cfg(package, bundle=None):
    docker = {"port": 8123}
    if bundle is not None:
        docker["bundle_content"] = bundle
    return {
        "deploy": {"name": "chan", "backend": "local-docker"},
        "bake": {"local_output_dir": str(package)},
        "infrastructure": {"docker": docker},
        "packaging": {"dvr_window_seconds": 20},
        "timeline": {"epoch_utc": "2026-02-03T04:05:06Z", "continuous": True},
    }


def test_volume_mode_is_default_and_mounts_the_package(env):
    package, fake = env
    ops.start(cfg(package), None, None)
    (run,) = fake.runs()
    assert f"{package}:/var/loop-package:ro" in run
    assert ops._IMAGE_TAG in run
    assert "/var/loop-package" in run[run.index("serve.py") + 1:][:1]
    assert len(fake.builds()) == 1  # only the shared code image


def test_bundle_mode_builds_per_channel_image_and_does_not_mount(env):
    package, fake = env
    ops.start(cfg(package, bundle=True), None, None)
    base_build, baked_build = fake.builds()
    assert "-f" in baked_build
    assert baked_build[baked_build.index("-f") + 1].endswith("Dockerfile.baked")
    assert f"BASE_IMAGE={ops._IMAGE_TAG}" in baked_build
    # the image's default serve flags: same builder as the local run, on the
    # image's fixed container port, with the configured epoch
    (defaults_arg,) = [a for a in baked_build if a.startswith("SERVE_ARGS_JSON=")]
    defaults = json.loads(defaults_arg.split("=", 1)[1])
    assert defaults == ops._serve_args(cfg(package, True), "chan", 8080, "2026-02-03T04:05:06Z")
    assert defaults[defaults.index("--port") + 1] == "8080"
    assert "--continuous-timeline" in defaults
    assert defaults[defaults.index("--dvr-window-seconds") + 1] == "20"
    assert baked_build[-1] == str(package)  # package dir is the build context
    assert baked_build[baked_build.index("-t") + 1] == "its-a-live-chan:baked"

    (run,) = fake.runs()
    assert "-v" not in run
    assert "its-a-live-chan:baked" in run
    # no positional package dir: the entrypoint finds the baked one itself
    assert run[run.index("serve.py") + 1] == "--epoch-utc"
    assert run[run.index("--epoch-utc") + 1] == "2026-02-03T04:05:06Z"
    assert run[run.index("--port") + 1] == "8123"
    assert run[run.index("--channel-name") + 1] == "chan"


def test_refresh_respects_bundle_mode(env):
    package, fake = env
    ops.refresh(cfg(package, bundle=True), None, None)
    assert "-v" not in fake.runs()[0]
    fake.calls.clear()
    ops.refresh(cfg(package, bundle=False), None, None)
    assert "-v" in fake.runs()[0]


def test_start_requires_a_baked_package(env, tmp_path):
    _, _fake = env
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit, match="spark"):
        ops.start(cfg(empty, bundle=True), None, None)


def test_image_command_builds_even_when_bundle_is_off_and_exports(env, tmp_path):
    package, fake = env
    out = tmp_path / "out" / "chan.tar"
    ops.image(cfg(package, bundle=False), None, "chan", ["--epoch-utc", "2026-01-01T00:00:00Z", "--save", str(out)])
    baked_build = fake.builds()[-1]
    (defaults_arg,) = [a for a in baked_build if a.startswith("SERVE_ARGS_JSON=")]
    assert json.loads(defaults_arg.split("=", 1)[1])[:2] == ["--epoch-utc", "2026-01-01T00:00:00Z"]
    (save,) = [c for c in fake.calls if c[:2] == ["docker", "save"]]
    assert save == ["docker", "save", "-o", str(out), "its-a-live-chan:baked"]
    assert out.read_bytes() == b"tar"
    assert not fake.runs()


def test_image_command_rejects_bad_args(env):
    package, _ = env
    with pytest.raises(SystemExit, match="Usage"):
        ops.image(cfg(package), None, "chan", ["--bogus"])


def test_status_reports_bundle_mode(env):
    package, fake = env
    fake.container_status = "running"
    result = ops.status(cfg(package, bundle=True), None, None)
    assert result["bundle_content"] is True
    assert result["image"] == "its-a-live-chan:baked"
    assert result["exported_image_bytes"] is None
